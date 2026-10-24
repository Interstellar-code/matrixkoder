// The setup wizard's first half (steps 1-5: Install, Data, Agents, Usage, Gates),
// moved out of wizard.tsx (plan wizards-redesign-r1, B1.1) so the two halves and
// the shared chrome can be rebuilt side by side.
//
// B1.2 redraws the five bodies as their Row 3 slides do at 46 columns: one Card
// per idea (the dashboard's own `Card` from tabs/kit), the question as the card's
// heading, a Chip for the answer's state and a dim line for the paths and hints.
// Every state the slides have no counterpart for — a repo that is not a git
// checkout, a `$MATRIXKODER_DATA` override, a `.gitignore` that already ignores
// the data dir, codexbar absent, nothing detected, an agent not on PATH — is
// drawn inside the same card rather than dropped, in the same voice.
//
// The field widgets the two halves share live here as well: wizard-steps-b.tsx
// imports them from this file, never the other way round, so there is no import
// cycle. Nothing here owns state: the draft is patched through `act.patch`,
// which wizard.tsx binds to its own (file-local) draft atom.
import type { Elements as ElementTables } from 'claude-code'
import type { MkWizardDraft } from '../types'
import { PALETTE, ROLE } from './tabs/palette'
import { Card, Chip } from './tabs/kit'
import { AGENT_NAMES, ZAI_KEY_CMD, dataDirOf, providerLabel } from './wizard-core'

/** Surfaces that draw Input and Select (mobile does not). */
export type WizardEls = ElementTables['terminal'] | ElementTables['desktop'] | ElementTables['vscode']

export type StrField = 'maxLanes' | 'claudeFiveHour' | 'claudeWeekly' | 'branch' | 'preauthorised' | 'qaStart' | 'qaDevUrl' | 'qaThemes' | 'qaMockups' | 'pollMin'
export type BoolField = 'gitignore' | 'skipPermissions' | 'qaFrontend' | 'autoOpen' | 'chip' | 'toasts' | 'replace'
export type GateFieldName = 'test' | 'typecheck' | 'lint' | 'build'

/** What a step body may do: patch the draft, re-check the existing yaml, write, close.
 *  wizard.tsx owns all four; the steps only call them. */
export type WizardAct = {
  patch: (p: Partial<MkWizardDraft> | ((cur: MkWizardDraft) => Partial<MkWizardDraft>)) => Promise<unknown>
  refresh: (p: Partial<MkWizardDraft>) => Promise<void>
  write: () => Promise<void>
  applyPrefs: () => Promise<void>
}

const YES_NO = [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }] as const

/** The four gate commands in the order step 5 draws — and the yaml writes — them. */
const GATE_FIELDS: readonly GateFieldName[] = ['test', 'typecheck', 'lint', 'build']

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

// ---------- small shared shapes (the slides' `.card`/`.hd`/`.chip`/`.dim` rows) ----------

/** A card's title row: the slide's `.hd` heading, bold (the agent cards build their own row, since
 *  their left column is a pressable Button and the state rides as a Chip beside it). */
function Head(els: WizardEls, label: string) {
  const { Text } = els
  return <Text bold={true}>{label}</Text>
}

// ---------- steps 1-5 ----------

/** Steps 1-5 of the wizard's body (Install, Data, Agents, Usage, Gates). */
export function StepsA(els: WizardEls, d: MkWizardDraft, act: WizardAct, step: number) {
  const { Box, Text, Button, Select } = els
  switch (step) {
    case 1:
      // Install: the mode as a chip, the root dim under it. Read-only — no field, nothing stored.
      return Card(els, {}, (
        <Box flexDirection="column">
          {Head(els, 'Install mode')}
          <Box flexDirection="row" gap={1}>
            {Chip(els, { label: d.installMode, role: 'ok' })}
            <Text>{d.installMode === 'dev' ? '(--plugin-dir)' : '(marketplace)'}</Text>
          </Box>
          <Text dimColor={true}>{d.pluginRoot || 'plugin root unknown'}</Text>
          <Text dimColor={true}>Info only — nothing is stored.</Text>
        </Box>
      ))
    case 2: {
      const dir = dataDirOf(d)
      const showGitignore = d.dataEnv == null && d.dataMode === 'repo'
      return (
        <Box flexDirection="column" gap={1}>
          {/* the map's "not a git repository" state: the code's own words in the same card style */}
          {Card(els, d.repo == null ? { role: 'warn' } : {}, (
            <Box flexDirection="column">
              {Head(els, 'Repo')}
              {d.repo == null
                ? <Text color={ROLE.warn.fg}>not a git repository</Text>
                : <Text color={PALETTE.grn}>{d.repo}</Text>}
            </Box>
          ))}
          {Card(els, d.dataEnv != null ? { role: 'warn' } : {}, (
            <Box flexDirection="column">
              {/* with $MATRIXKODER_DATA set the mode is fixed by the environment: the warn line replaces the Select */}
              {Head(els, 'Data location')}
              {d.dataEnv != null
                ? <Text color={ROLE.warn.fg}>{`$MATRIXKODER_DATA is set: data goes to ${d.dataEnv}`}</Text>
                : (
                  <Select
                    key="wz-datamode" label="Data location" value={d.dataMode}
                    options={[{ value: 'repo', label: '<repo>/.matrixkoder/ (default)' }, { value: 'home', label: `${d.globalDir ?? '~/.matrixkoder'}/projects/${d.slug}/` }]}
                    onSelect={v => act.refresh({ dataMode: v === 'home' ? 'home' : 'repo' })}
                  />
                )}
              <Text dimColor={true}>Data dir: {dir ?? '—'}</Text>
            </Box>
          ))}
          {showGitignore && Card(els, {}, (
            <Box flexDirection="column">
              {Head(els, '.gitignore')}
              {/* the map's ".gitignore already ignores" state: no question to ask, so a dim confirmation */}
              {d.gitignoreHas
                ? <Text dimColor={true}>.gitignore already ignores /.matrixkoder/</Text>
                : YesNo(els, d, act, 'gitignore', 'Add /.matrixkoder/ to .gitignore')}
            </Box>
          ))}
        </Box>
      )
    }
    case 3:
      return (
        <Box flexDirection="column" gap={1}>
          {/* the code's own caption kept above the cards: it is what tells the reader the rows are tappable */}
          <Text dimColor={true}>Agents (tick to enable)</Text>
          {AGENT_NAMES.map(n => {
            const a = d.agents[n]
            const digit = String(toggleable.indexOf(n) + 1) // hotkey 1..4
            // the map's "not found" state: the code's own row, verbatim, inside the same card
            if (a == null || !a.found) {
              return <Box key={n}>{Card(els, {}, <Text dimColor={true}>{`[ ] ${n}  not found`}</Text>)}</Box>
            }
            const models = a.models.length
            return (
              <Box key={n} flexDirection="column">
                {Card(els, a.enabled ? {} : { edge: PALETTE.edge }, (
                  <Box flexDirection="column">
                    <Box flexDirection="row" justifyContent="space-between" gap={1}>
                      {n === 'claude'
                        ? <Text bold={true}>{`[x] ${n}`}</Text>
                        : (
                          <Button
                            key={`wz-agent-${n}`} hotkey={digit} label={`${digit} ${a.enabled ? '[x]' : '[ ]'} ${n}`}
                            onPress={() => act.patch(cur => ({ agents: { ...cur.agents, [n]: { ...a, enabled: !a.enabled } } }))}
                          />
                        )}
                      {Chip(els, n === 'claude'
                        ? { label: 'always on', role: 'live' }
                        : { label: a.enabled ? 'enabled' : 'off', role: a.enabled ? 'ok' : 'idle' })}
                    </Box>
                    <Text dimColor={true}>{a.path ?? 'path unknown'}</Text>
                    {models > 0 && <Text dimColor={true}>{`${models} models: ${a.models.slice(0, 3).join(', ')}${models > 3 ? ', …' : ''}`}</Text>}
                  </Box>
                ))}
              </Box>
            )
          })}
          {/* the herdr plugin card: the two slide options, the linked confirmation, or the dim "not on PATH" state */}
          {Card(els, d.herdrPlugin === 'link' && d.agents.herdr?.found ? { role: 'live' } : {}, (
            <Box flexDirection="column">
              {Head(els, 'herdr plugin')}
              {!d.agents.herdr?.found
                ? <Text dimColor={true}>herdr not found on PATH</Text>
                : d.herdrPlugin === 'linked'
                  ? <Text color={ROLE.ok.fg}>✓ MatrixKoder herdr plugin is linked</Text>
                  : (
                    <Box flexDirection="column">
                      {d.herdrStaleLink != null && (
                        <Text color={ROLE.warn.fg}>{`herdr link is stale (${d.herdrStaleLink})`}</Text>
                      )}
                      <Select
                        key="wz-herdrPlugin" label="herdr plugin"
                        options={[{ value: 'link', label: d.herdrStaleLink != null ? 'relink it now' : 'link it now' }, { value: 'skip', label: 'skip' }]}
                        value={d.herdrPlugin}
                        onSelect={v => act.patch({ herdrPlugin: v as 'link' | 'skip' })}
                      />
                    </Box>
                  )}
            </Box>
          ))}
        </Box>
      )
    case 4: {
      const missing = d.codexbarVersion == null
      return (
        <Box flexDirection="column" gap={1}>
          {Card(els, missing ? { role: 'warn' } : {}, (
            <Box flexDirection="column">
              {Head(els, 'CodexBar')}
              {missing
                ? (
                  <Box flexDirection="column">
                    <Text color={ROLE.warn.fg}>⚠ codexbar not found on PATH</Text>
                    <Text dimColor={true}>Install CodexBar (github.com/steipete/CodexBar) and put its CLI on PATH; usage stays n/a until then. You can continue.</Text>
                  </Box>
                )
                : <Text color={ROLE.ok.fg}>{`✓ codexbar ${d.codexbarVersion}`}</Text>}
            </Box>
          ))}
          {/* nothing to tick when CodexBar is absent: the providers list comes from its own reply */}
          {!missing && Card(els, {}, (
            <Box flexDirection="column">
              {Head(els, 'Providers (tick to track)')}
              {d.providersAll.map((p, i) => {
                const on = d.providersOn.includes(p)
                return (
                  <Box key={p} flexDirection="column">
                    <Button
                      key={`wz-prov-${p}`} {...(i < 9 ? { hotkey: String(i + 1) } : {})} label={`${i < 9 ? `${i + 1} ` : ''}${on ? '[x]' : '[ ]'} ${providerLabel(p)}`}
                      onPress={() => act.patch(cur => ({ providersOn: cur.providersOn.includes(p) ? cur.providersOn.filter(x => x !== p) : [...cur.providersOn, p] }))}
                    />
                    {/* B-9: a listed-but-unticked provider says why in one dim line. The draft keeps only the
                        answer, never CodexBar's own enabled flag, so the line names what untracked costs —
                        true whether CodexBar listed the provider disabled or the user unticked it. */}
                    {!on && <Text dimColor={true}>{`${providerLabel(p)}: not tracked — its usage is not read; tick it to track.`}</Text>}
                  </Box>
                )
              })}
            </Box>
          ))}
          {Card(els, { role: 'warn' }, (
            <Box flexDirection="column">
              {Head(els, 'z.ai key (OpenCode plan)')}
              <Text dimColor={true}>The wizard never asks for or stores API keys. In your own terminal run:</Text>
              <Box backgroundColor={PALETTE.field} paddingX={1}><Text>{ZAI_KEY_CMD}</Text></Box>
            </Box>
          ))}
        </Box>
      )
    }
    default: {
      // step 5 — nothing detected: the first Input takes the initial focus so the step is typeable at once
      const gatesEmpty = d.gatesFrom === ''
      // B-11: an empty command is a real answer (none for this repo), so it never blocks Next; it is
      // named here and written as `""` (the emitter's `inline('')`).
      const empty = GATE_FIELDS.filter(f => d.gates[f].trim() === '')
      return (
        <Box flexDirection="column" gap={1}>
          {Card(els, {}, (
            <Box flexDirection="column">
              {Head(els, 'Commands')}
              <Text dimColor={true}>{d.gatesFrom !== '' ? `Proposed from ${d.gatesFrom}. Test suites are not run.` : 'Nothing detected — Tab to a field and type the command you use.'}</Text>
              {GateField(els, d, act, 'test', 'test', gatesEmpty)}
              {GateField(els, d, act, 'typecheck', 'typecheck')}
              {GateField(els, d, act, 'lint', 'lint')}
              {GateField(els, d, act, 'build', 'build')}
              {empty.length > 0 && <Text dimColor={true}>{`empty gates: ${empty.join(', ')} — written as "" (no command).`}</Text>}
            </Box>
          ))}
          {Card(els, {}, (
            <Box flexDirection="column">
              {Head(els, 'Baselines and banned')}
              <Text dimColor={true}>Baselines (type errors, lint errors, failing tests): measured at first run.</Text>
              {d.banned.length > 0 && <Text dimColor={true}>{`Banned (dev servers): ${d.banned.join(', ')}`}</Text>}
            </Box>
          ))}
        </Box>
      )
    }
  }
}
