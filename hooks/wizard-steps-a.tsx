// The setup wizard's first half (steps 1-5: Install, Data, Agents, Usage, Gates),
// moved out of wizard.tsx (plan wizards-redesign-r1, B1.1) so the two halves and
// the shared chrome can be rebuilt side by side.
//
// The field widgets the two halves share live here as well: wizard-steps-b.tsx
// imports them from this file, never the other way round, so there is no import
// cycle. Nothing here owns state: the draft is patched through `act.patch`,
// which wizard.tsx binds to its own (file-local) draft atom.
import type { Elements as ElementTables, EngineInterface } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { ROLE } from './tabs/palette'
import { AGENT_NAMES, ZAI_KEY_CMD, dataDirOf, providerLabel } from './wizard-core'

/** Surfaces that draw Input and Select (mobile does not). */
export type WizardEls = ElementTables['terminal'] | ElementTables['desktop'] | ElementTables['vscode']

export type StrField = 'maxLanes' | 'claudeFiveHour' | 'claudeWeekly' | 'branch' | 'preauthorised' | 'qaStart' | 'qaDevUrl' | 'qaThemes' | 'qaMockups' | 'pollMin'
export type BoolField = 'gitignore' | 'skipPermissions' | 'qaFrontend' | 'autoOpen' | 'chip' | 'toasts' | 'replace'
export type GateFieldName = 'install' | 'test' | 'typecheck' | 'lint_changed' | 'format_changed'

/** What a step body may do: patch the draft, re-check the existing yaml, write, close.
 *  wizard.tsx owns all four; the steps only call them. */
export type WizardAct = {
  patch: (p: Partial<MkWizardDraft> | ((cur: MkWizardDraft) => Partial<MkWizardDraft>)) => Promise<unknown>
  refresh: (p: Partial<MkWizardDraft>) => Promise<void>
  write: () => Promise<void>
  applyPrefs: () => Promise<void>
}

const YES_NO = [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }] as const

/** the agents the step-3 hotkeys 1..4 toggle (claude is always on and drawn as a line) */
const toggleable: readonly string[] = AGENT_NAMES.filter(n => n !== 'claude')

// ---------- field widgets ----------

export function TextField(els: WizardEls, d: MkWizardDraft, act: WizardAct, field: StrField, label: string, placeholder = '') {
  const { Input } = els
  const set = (v: string): Promise<unknown> => act.patch({ [field]: v })
  return <Input key={`wz-${field}`} label={label} value={d[field]} placeholder={placeholder} onInput={v => set(v)} onSubmit={v => set(v)} />
}

export function GateField(els: WizardEls, d: MkWizardDraft, act: WizardAct, field: GateFieldName, label: string, autoFocus = false) {
  const { Input } = els
  const set = (v: string): Promise<unknown> => act.patch(cur => ({ gates: { ...cur.gates, [field]: v } }))
  return <Input key={`wz-gate-${field}`} label={label} value={d.gates[field]} placeholder="(none)" autoFocus={autoFocus || undefined} onInput={v => set(v)} onSubmit={v => set(v)} />
}

export function YesNo(els: WizardEls, d: MkWizardDraft, act: WizardAct, field: BoolField, label: string) {
  const { Select } = els
  return (
    <Select
      key={`wz-${field}`} label={label} options={YES_NO} value={d[field] ? 'yes' : 'no'}
      onSelect={v => act.patch({ [field]: v === 'yes' })}
    />
  )
}

export function Choice(els: WizardEls, d: MkWizardDraft, act: WizardAct, field: StrField, label: string, values: readonly string[]) {
  const { Select } = els
  return (
    <Select
      key={`wz-${field}`} label={label} options={values.map(v => ({ value: v }))} value={d[field]}
      onSelect={v => act.patch({ [field]: v })}
    />
  )
}

function HerdrLinkChoice(els: WizardEls, d: MkWizardDraft, act: WizardAct) {
  const { Box, Text, Select } = els
  const a = d.agents.herdr
  const label = 'Link the MatrixKoder herdr plugin (herdr pushes lane events instead of polling)'
  const options = YES_NO.map(o => ({ value: o.value === 'yes' ? 'link' : 'skip', label: o.label }))
  if (!a?.found) return <Text dimColor={true}>herdr not found on PATH</Text>
  if (d.herdrPlugin === 'linked') return <Text color={ROLE.ok.fg}>✓ MatrixKoder herdr plugin is linked</Text>
  return (
    <Box flexDirection="column">
      <Select
        key="wz-herdrPlugin" label={label} options={options} value={d.herdrPlugin}
        onSelect={v => act.patch({ herdrPlugin: v as 'link' | 'skip' })}
      />
    </Box>
  )
}

// ---------- steps 1-5 ----------

/** Steps 1-5 of the wizard's body (Install, Data, Agents, Usage, Gates). */
export function StepsA(els: WizardEls, d: MkWizardDraft, act: WizardAct, step: number) {
  const { Box, Text, Button, Select } = els
  switch (step) {
    case 1:
      return (
        <Box flexDirection="column">
          <Text>Install mode: <Text bold={true}>{d.installMode}</Text>{d.installMode === 'dev' ? ' (--plugin-dir)' : ' (marketplace)'}</Text>
          <Text dimColor={true}>{d.pluginRoot || 'plugin root unknown'}</Text>
          <Text dimColor={true}>Info only — nothing is stored.</Text>
        </Box>
      )
    case 2: {
      const dir = dataDirOf(d)
      return (
        <Box flexDirection="column" gap={1}>
          <Text>Repo: <Text bold={true}>{d.repo ?? 'not a git repository'}</Text></Text>
          {d.dataEnv != null
            ? <Text color={ROLE.warn.fg}>{`$MATRIXKODER_DATA is set: data goes to ${d.dataEnv}`}</Text>
            : (
              <Select
                key="wz-datamode" label="Data location" value={d.dataMode}
                options={[{ value: 'repo', label: '<repo>/.matrixkoder/ (default)' }, { value: 'home', label: `${d.globalDir ?? '~/.matrixkoder'}/projects/${d.slug}/` }]}
                onSelect={v => act.refresh({ dataMode: v === 'home' ? 'home' : 'repo' })}
              />
            )}
          {d.dataEnv == null && d.dataMode === 'repo' && (d.gitignoreHas
            ? <Text dimColor={true}>.gitignore already ignores /.matrixkoder/</Text>
            : YesNo(els, d, act, 'gitignore', 'Add /.matrixkoder/ to .gitignore'))}
          <Text dimColor={true}>Data dir: {dir ?? '—'}</Text>
        </Box>
      )
    }
    case 3:
      return (
        <Box flexDirection="column">
          <Text bold={true}>Agents (tick to enable)</Text>
          {AGENT_NAMES.map(n => {
            const a = d.agents[n]
            const digit = String(toggleable.indexOf(n) + 1) // hotkey 1..4
            if (a == null || !a.found) return <Text key={n} dimColor={true}>{`[ ] ${n}  not found`}</Text>
            if (n === 'claude') return <Text key={n}>{`[x] ${n}  ${a.path ?? ''}`}</Text>
            return (
              <Box key={n} flexDirection="column">
                <Button
                  key={`wz-agent-${n}`} hotkey={digit} label={`${digit} ${a.enabled ? '[x]' : '[ ]'} ${n}  ${a.path ?? ''}`}
                  onPress={() => act.patch(cur => ({ agents: { ...cur.agents, [n]: { ...a, enabled: !a.enabled } } }))}
                />
                {a.models.length > 0 && <Text dimColor={true}>{`    ${a.models.length} models: ${a.models.slice(0, 3).join(', ')}${a.models.length > 3 ? ', …' : ''}`}</Text>}
              </Box>
            )
          })}
          {d.agents.herdr?.found && <Box flexDirection="column" marginTop={1}>{HerdrLinkChoice(els, d, act)}</Box>}
        </Box>
      )
    case 4:
      return (
        <Box flexDirection="column" gap={1}>
          {d.codexbarVersion == null
            ? (
              <Box flexDirection="column">
                <Text color={ROLE.warn.fg}>⚠ codexbar not found on PATH</Text>
                <Text dimColor={true}>Install CodexBar (github.com/steipete/CodexBar) and put its CLI on PATH; usage stays n/a until then. You can continue.</Text>
              </Box>
            )
            : <Text color={ROLE.ok.fg}>{`✓ codexbar ${d.codexbarVersion}`}</Text>}
          {d.codexbarVersion != null && (
            <Box flexDirection="column">
              <Text bold={true}>Providers (tick to track)</Text>
              {d.providersAll.map((p, i) => (
                <Button
                  key={`wz-prov-${p}`} {...(i < 9 ? { hotkey: String(i + 1) } : {})} label={`${i < 9 ? `${i + 1} ` : ''}${d.providersOn.includes(p) ? '[x]' : '[ ]'} ${providerLabel(p)}`}
                  onPress={() => act.patch(cur => ({ providersOn: cur.providersOn.includes(p) ? cur.providersOn.filter(x => x !== p) : [...cur.providersOn, p] }))}
                />
              ))}
            </Box>
          )}
          <Box flexDirection="column">
            <Text bold={true}>z.ai key (OpenCode plan)</Text>
            <Text dimColor={true}>The wizard never asks for or stores API keys. In your own terminal run:</Text>
            <Text>{ZAI_KEY_CMD}</Text>
          </Box>
        </Box>
      )
    default: {
      // step 5 — nothing detected: the first Input takes the initial focus so the step is typeable at once
      const gatesEmpty = d.gatesFrom === ''
      return (
        <Box flexDirection="column">
          <Text dimColor={true}>{d.gatesFrom !== '' ? `Proposed from ${d.gatesFrom}. Test suites are not run.` : 'Nothing detected — Tab to a field and type the command you use.'}</Text>
          {GateField(els, d, act, 'install', 'install', gatesEmpty)}
          {GateField(els, d, act, 'test', 'test')}
          {GateField(els, d, act, 'typecheck', 'typecheck')}
          {GateField(els, d, act, 'lint_changed', 'lint_changed')}
          {GateField(els, d, act, 'format_changed', 'format_changed')}
          <Text dimColor={true}>Baselines (type errors, lint errors, failing tests): measured at first run.</Text>
          {d.banned.length > 0 && <Text dimColor={true}>{`Banned (dev servers): ${d.banned.join(', ')}`}</Text>}
        </Box>
      )
    }
  }
}
