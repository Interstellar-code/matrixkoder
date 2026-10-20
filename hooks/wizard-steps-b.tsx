// The setup wizard's second half (steps 6-10: Routing, Autonomy, QA, Dashboard,
// Review), moved out of wizard.tsx (plan wizards-redesign-r1, B1.1) so the two
// halves and the shared chrome can be rebuilt side by side. The shared field
// widgets and the `WizardAct` the steps call live in wizard-steps-a.tsx; this
// file imports them, so nothing imports this one back.
import type { MkWizardDraft } from '../types'
import { ROLE } from './tabs/palette'
import { Choice, TextField, YesNo, type WizardAct, type WizardEls } from './wizard-steps-a'
import { buildYaml, builderOrderOf, dataDirOf } from './wizard-core'

/** How many preview lines the review draws before it cuts to "… N more lines". */
export const PREVIEW_LINES = 40

function Routing(els: WizardEls, d: MkWizardDraft, act: WizardAct) {
  const { Box, Input } = els
  const setOrder = (v: string): Promise<unknown> => act.patch({ builderOrder: v })
  return (
    <Box flexDirection="column">
      <Input key="wz-builderOrder" label="Builder order" value={builderOrderOf(d).join(', ')} onInput={v => setOrder(v)} onSubmit={v => setOrder(v)} />
      {Choice(els, d, act, 'maxLanes', 'Max build lanes', ['1', '2', '3', '4', '5'])}
      {Choice(els, d, act, 'claudeFiveHour', 'Claude reserve floor, 5h left %', ['20', '40', '60'])}
      {Choice(els, d, act, 'claudeWeekly', 'Claude reserve floor, weekly left %', ['20', '30', '50'])}
      {TextField(els, d, act, 'branch', 'Integration branch')}
    </Box>
  )
}

function Review(els: WizardEls, d: MkWizardDraft, act: WizardAct, oldYaml: string | null) {
  const { Box, Text, Button, Select } = els
  const data = dataDirOf(d)
  if (d.phase === 'written') {
    return (
      <Box flexDirection="column">
        <Text color={ROLE.ok.fg} bold={true}>✓ MatrixKoder is set up</Text>
        {d.written.map(p => <Text key={p} dimColor={true}>{`wrote ${p}`}</Text>)}
        {d.existingYaml && !d.replace && <Text dimColor={true}>kept existing matrixkoder.yaml</Text>}
        {d.herdrLinkResult && (
          <Text color={d.herdrLinkResult.ok ? 'green' : 'yellow'}>
            {`${d.herdrLinkResult.ok ? '✓' : '⚠'} ${d.herdrLinkResult.text}`}
          </Text>
        )}
        {d.installMode === 'marketplace' && (
          <Text dimColor={true}>Re-run /mk setup after each MatrixKoder update: the herdr link points at this plugin version's folder.</Text>
        )}
        {d.error != null && <Text color={ROLE.warn.fg}>{`⚠ ${d.error}`}</Text>}
        <Text>Next: /mk to open the dashboard.</Text>
      </Box>
    )
  }
  const yaml = buildYaml(d, 'pending', d.existingYaml && d.replace ? oldYaml : null).split('\n')
  return (
    <Box flexDirection="column" gap={1}>
      {d.phase === 'error' && <Text color={ROLE.err.fg} bold={true}>{`✗ ${d.error ?? 'write failed'}`}</Text>}
      <Box flexDirection="column">
        <Text bold={true}>Will write</Text>
        <Text>{`${d.existingYaml && !d.replace ? 'keep   ' : d.existingYaml ? 'replace' : 'create '} ${data ?? '?'}/matrixkoder.yaml`}</Text>
        <Text>{`create  ${data ?? '?'}/plans/ and runs/`}</Text>
        {d.dataEnv == null && d.dataMode === 'repo' && d.gitignore && !d.gitignoreHas && <Text>add     /.matrixkoder/ to .gitignore</Text>}
        <Text>{`${d.globalDir ?? '~/.matrixkoder'} root projects table (register this repo)`}</Text>
        {d.agents.herdr?.found && d.herdrPlugin === 'link' && <Text>{`link    ${d.pluginRoot}/herdr in herdr`}</Text>}
        {d.agents.herdr?.found && d.herdrPlugin === 'linked' && <Text dimColor={true}>herdr plugin already linked</Text>}
        {d.installMode === 'marketplace' && <Text dimColor={true}>Re-run /mk setup after each MatrixKoder update: the herdr link points at this plugin version's folder.</Text>}
      </Box>
      {d.existingYaml && (
        <Select
          key="wz-replace" label="matrixkoder.yaml already exists" value={d.replace ? 'replace' : 'keep'}
          options={[{ value: 'keep', label: 'keep it (default)' }, { value: 'replace', label: 'replace it' }]}
          onSelect={v => act.patch({ replace: v === 'replace' })}
        />
      )}
      <Box flexDirection="column">
        <Text bold={true}>matrixkoder.yaml preview</Text>
        {yaml.slice(0, PREVIEW_LINES).map((l, i) => <Text key={`y${i}`} dimColor={true}>{l}</Text>)}
        {yaml.length > PREVIEW_LINES && <Text dimColor={true}>{`… ${yaml.length - PREVIEW_LINES} more lines`}</Text>}
      </Box>
      <Button key="wz-write" variant="primary" hotkey="w" label="w Write these" onPress={() => act.write()} />
    </Box>
  )
}

/** Steps 6-10 of the wizard's body (Routing, Autonomy, QA, Dashboard, Review). */
export function StepsB(els: WizardEls, d: MkWizardDraft, act: WizardAct, step: number, options: { autoOpen?: boolean; chip?: boolean; toasts?: boolean }, oldYaml: string | null) {
  const { Box, Text, Button, Select } = els
  switch (step) {
    case 6:
      return Routing(els, d, act)
    case 7:
      return (
        <Box flexDirection="column">
          {YesNo(els, d, act, 'skipPermissions', 'Builders skip permission prompts (own worktree only)')}
          {TextField(els, d, act, 'preauthorised', 'Pre-authorised')}
        </Box>
      )
    case 8:
      return (
        <Box flexDirection="column">
          {YesNo(els, d, act, 'qaFrontend', 'Has a frontend')}
          {TextField(els, d, act, 'qaDevUrl', 'Dev URL', 'https://localhost:3000')}
          {TextField(els, d, act, 'qaStart', 'Start command (orchestrator only)')}
          {TextField(els, d, act, 'qaThemes', 'Themes (comma separated)')}
          {TextField(els, d, act, 'qaMockups', 'Mockups dir')}
          <Select
            key="wz-qaTarget" label="QA integration target" value={d.qaTarget}
            options={[{ value: 'local-main', label: 'local-main (QA on the running dev server)' }, { value: 'branch', label: 'branch (separate build)' }]}
            onSelect={v => act.patch({ qaTarget: v === 'branch' ? 'branch' : 'local-main' })}
          />
        </Box>
      )
    case 9:
      return (
        <Box flexDirection="column">
          {YesNo(els, d, act, 'autoOpen', 'Auto-open the dashboard (fullscreen, ≥144 cols)')}
          {YesNo(els, d, act, 'chip', 'Status chip')}
          {YesNo(els, d, act, 'toasts', 'Toasts')}
          {TextField(els, d, act, 'pollMin', 'Usage poll (minutes)')}
          <Text dimColor={true}>{`Plugin settings today: autoOpen ${options.autoOpen !== false ? 'on' : 'off'}, chip ${options.chip !== false ? 'on' : 'off'}, toasts ${options.toasts !== false ? 'on' : 'off'}.`}</Text>
          <Text dimColor={true}>These live in the plugin's own settings (claude plugin configure matrixkoder), not in the yaml.</Text>
          {d.error != null && <Text color={ROLE.err.fg}>{`✗ ${d.error}`}</Text>}
          <Button key="wz-dash-apply" variant="primary" label="Apply these settings" onPress={() => act.applyPrefs()} />
        </Box>
      )
    default:
      return Review(els, d, act, oldYaml)
  }
}
