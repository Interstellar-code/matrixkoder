// The setup wizard's second half (steps 6-10: Routing, Autonomy, QA, Dashboard,
// Review), moved out of wizard.tsx (plan wizards-redesign-r1, B1.1) so the two
// halves and the shared chrome can be rebuilt side by side. The shared field
// widgets and the `WizardAct` the steps call live in wizard-steps-a.tsx; this
// file imports them, so nothing imports this one back.
//
// Each step draws the cards its approved slide draws (Row 3, `design/setup/
// SetupRouting..SetupWritten.html`): a bold card heading, the control with the
// slide's dim label, and the slide's own trailing note. Every state a slide has
// no row for (a branch that cannot be written, a poll that falls back to 5, a
// builder order naming an agent that is off) is one dim line in the same card:
// B-11 validates and names, and never blocks the step.
import type { MkWizardDraft } from '../types'
import { ROLE } from './tabs/palette'
import { Choice, TextField, YesNo, type WizardAct, type WizardEls } from './wizard-steps-a'
import { Card } from './tabs/kit'
import { buildYaml, builderOrderOf, dataDirOf, enabledAgents, splitList, yamlDiff } from './wizard-core'

/** A card's bold heading (the slide's `.hd` in the card's title row). */
function Hd(els: WizardEls, label: string) {
  const { Text } = els
  return <Text bold={true}>{label}</Text>
}

/** A dim line: the slide's `.dim`, and the one place a state the slide lacks is named. */
function Dim(els: WizardEls, text: string) {
  const { Text } = els
  return <Text dimColor={true}>{text}</Text>
}

/** The whole usage poll the step-10 write stores: an integer ≥ 1, else the 5 applyPrefs falls back to. */
function pollValue(pollMin: string): { value: number; ok: boolean } {
  const text = pollMin.trim()
  const n = /^\d+$/.test(text) ? Number(text) : Number.NaN
  return Number.isFinite(n) && n >= 1 ? { value: n, ok: true } : { value: 5, ok: false }
}

function Routing(els: WizardEls, d: MkWizardDraft, act: WizardAct) {
  const { Box, Input, Text } = els
  const setOrder = (v: string): Promise<unknown> => act.patch({ builderOrder: v })
  // B-7: the field draws the user's own text while they type (the draft holds it raw), never the
  // re-joined order: `builderOrderOf` splits on commas, so a trailing ", " was dropped on every
  // keystroke and `codex, agy` could not be typed one character at a time. It stays the one reader
  // at emit time (buildYaml's `builder_order`), so an empty field still writes the derived order.
  const raw = d.builderOrder ?? builderOrderOf(d).join(', ')
  const on = enabledAgents(d)
  const strangers = splitList(raw).filter(n => !on.includes(n))
  return (
    <Box flexDirection="column" gap={1}>
      {Card(els, {}, (
        <Box flexDirection="column">
          {Hd(els, 'Builder order')}
          <Input key="wz-builderOrder" label="Builder order" value={raw} onInput={v => setOrder(v)} onSubmit={v => setOrder(v)} />
          {strangers.length > 0 && <Text dimColor={true}>{`⚠ not an enabled agent: ${strangers.join(', ')} — written as typed`}</Text>}
        </Box>
      ))}
      {Card(els, {}, (
        <Box flexDirection="column">
          {Hd(els, 'Lanes')}
          {Choice(els, d, act, 'maxLanes', 'Max build lanes', ['1', '2', '3', '4', '5'])}
        </Box>
      ))}
      {Card(els, {}, (
        <Box flexDirection="column">
          {Hd(els, 'Claude reserve floor')}
          {Choice(els, d, act, 'claudeFiveHour', '5h left %', ['20', '40', '60'])}
          {Choice(els, d, act, 'claudeWeekly', 'weekly left %', ['20', '30', '50'])}
        </Box>
      ))}
      {Card(els, {}, (
        <Box flexDirection="column">
          {Hd(els, 'Branch')}
          {TextField(els, d, act, 'branch', 'Integration branch')}
          {d.branch.trim() === ''
            ? Dim(els, 'the integration branch is empty — written as ""  (lanes merge here)')
            : /\s/.test(d.branch) && Dim(els, `the integration branch cannot hold spaces — written as ${JSON.stringify(d.branch)}`)}
        </Box>
      ))}
    </Box>
  )
}

/** The whole file, drawn: the pane's own window scrolls it. B-6: the old form cut at 40 lines and
 *  drew `… N more lines` over text built with `checked_at: pending`, so the preview could not match
 *  the write. Both halves now come from one `buildYaml` call and one clock read. */
function Preview(els: WizardEls, title: string, right: string, rows: readonly string[], footer: string, key: string) {
  const { Box, Text } = els
  return Card(els, {}, (
    <Box flexDirection="column">
      <Box flexDirection="row" justifyContent="space-between" gap={1}>
        <Text bold={true}>{title}</Text>
        {right !== '' && <Text dimColor={true}>{right}</Text>}
      </Box>
      {rows.map((l, i) => <Text key={`${key}${String(i)}`} dimColor={true}>{l}</Text>)}
      {footer !== '' && <Text dimColor={true}>{footer}</Text>}
    </Box>
  ))
}

function Review(els: WizardEls, d: MkWizardDraft, act: WizardAct, oldYaml: string | null, checkedAt: string) {
  const { Box, Text, Select } = els
  const data = dataDirOf(d)
  const yamlName = d.migrated ? 'project.yaml' : 'matrixkoder.yaml'
  const yamlPath = data == null ? null : `${data}/${yamlName}`
  if (d.phase === 'written') {
    return (
      <Box flexDirection="column" gap={1}>
        <Text color={ROLE.ok.fg} bold={true}>✓ MatrixKoder is set up</Text>
        {Card(els, {}, (
          <Box flexDirection="column">
            {d.written.map(p => (
              <Box key={p} flexDirection="column">
                <Text dimColor={true}>{`wrote ${p}`}</Text>
                {/* mkdir -p makes both folders and `written` never named them (map §7): the row the slide draws */}
                {data != null && p === yamlPath && <Text dimColor={true}>{`wrote ${data}/plans/ and runs/`}</Text>}
              </Box>
            ))}
            {d.existingYaml && !d.replace && <Text dimColor={true}>kept existing yaml (unchanged)</Text>}
            {d.herdrLinkResult && (
              <Text color={d.herdrLinkResult.ok ? ROLE.ok.fg : ROLE.warn.fg}>
                {`${d.herdrLinkResult.ok ? '✓' : '⚠'} ${d.herdrLinkResult.text}`}
              </Text>
            )}
            {d.error != null && <Text color={ROLE.warn.fg}>{`⚠ ${d.error}`}</Text>}
            {d.prefsSaved && <Text dimColor={true}>set 4 dashboard prefs</Text>}
          </Box>
        ))}
        {d.installMode === 'marketplace' && (
          <Text dimColor={true}>Re-run /mk setup after each MatrixKoder update: the herdr link points at this plugin version's folder.</Text>
        )}
        <Text>Next: /mk to open the dashboard.</Text>
      </Box>
    )
  }
  const newText = buildYaml(d, checkedAt, d.existingYaml && d.replace ? oldYaml : null)
  const yamlRows = newText.replace(/\n$/, '').split('\n')
  const diff = d.existingYaml && d.replace ? yamlDiff(oldYaml, newText) : null
  return (
    <Box flexDirection="column" gap={1}>
      {d.phase === 'error' && <Text color={ROLE.err.fg} bold={true}>{`✗ ${d.error ?? 'write failed'}`}</Text>}
      {Card(els, {}, (
        <Box flexDirection="column">
          {Hd(els, 'Will write')}
          <Text>{`${d.existingYaml && !d.replace ? 'keep   ' : d.existingYaml ? 'replace' : 'create '} ${yamlPath ?? '?'}`}</Text>
          <Text>{`create  ${data ?? '?'}/plans/ and runs/`}</Text>
          {d.dataEnv == null && d.dataMode === 'repo' && d.gitignore && !d.gitignoreHas && <Text>add     /.matrixkoder/ to .gitignore</Text>}
          <Text>{`register this repo in the root projects table`}</Text>
          {d.agents.herdr?.found && d.herdrPlugin === 'link' && <Text>{`link    ${d.pluginRoot}/herdr in herdr`}</Text>}
          {d.agents.herdr?.found && d.herdrPlugin === 'linked' && Dim(els, 'herdr plugin already linked')}
          <Text>set     4 dashboard prefs (plugin settings)</Text>
        </Box>
      ))}
      {d.existingYaml && (
        <Select
          key="wz-replace" label={`${yamlName} already exists`} value={d.replace ? 'replace' : 'keep'}
          options={[{ value: 'keep', label: 'keep it (default)' }, { value: 'replace', label: 'replace it' }]}
          onSelect={v => act.patch({ replace: v === 'replace' })}
        />
      )}
      {diff == null
        ? Preview(els, `${yamlName} · new file`, 'exactly what is written', yamlRows.map(l => `+ ${l}`), `↑↓ scroll · all ${String(yamlRows.length)} lines`, 'y')
        : Preview(
          els, 'What a replace changes', 'diff vs your file', diff.rows.map(r => `${r.sign} ${r.text}`),
          `${String(diff.changed)} ${diff.changed === 1 ? 'line' : 'lines'} changed · ${String(diff.unchanged)} unchanged · ↑↓ scroll`, 'd',
        )}
      {d.installMode === 'marketplace' && <Text dimColor={true}>Re-run /mk setup after each MatrixKoder update: the herdr link points at this plugin version's folder.</Text>}
    </Box>
  )
}

/** Steps 6-10 of the wizard's body (Routing, Autonomy, QA, Dashboard, Review). */
export function StepsB(els: WizardEls, d: MkWizardDraft, act: WizardAct, step: number, options: { autoOpen?: boolean; chip?: boolean; toasts?: boolean }, oldYaml: string | null, checkedAt: string) {
  const { Box, Text, Select } = els
  switch (step) {
    case 6:
      return Routing(els, d, act)
    case 7:
      return (
        <Box flexDirection="column" gap={1}>
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Permissions')}
              {YesNo(els, d, act, 'skipPermissions', 'Builders skip permission prompts')}
              {Dim(els, 'Own worktree only.')}
            </Box>
          ))}
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Pre-authorised')}
              {TextField(els, d, act, 'preauthorised', 'Pre-authorised')}
            </Box>
          ))}
        </Box>
      )
    case 8:
      return (
        <Box flexDirection="column" gap={1}>
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Frontend')}
              {YesNo(els, d, act, 'qaFrontend', 'Has a frontend')}
            </Box>
          ))}
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Run')}
              {TextField(els, d, act, 'qaDevUrl', 'Dev URL', 'https://localhost:3000')}
              {TextField(els, d, act, 'qaStart', 'Start command (orchestrator only)')}
            </Box>
          ))}
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Check')}
              {TextField(els, d, act, 'qaThemes', 'Themes (comma separated)')}
              {TextField(els, d, act, 'qaMockups', 'Mockups dir')}
            </Box>
          ))}
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'QA integration target')}
              <Select
                key="wz-qaTarget" value={d.qaTarget}
                options={[{ value: 'local-main' }, { value: 'branch' }]}
                onSelect={v => act.patch({ qaTarget: v === 'branch' ? 'branch' : 'local-main' })}
              />
              {Dim(els, 'local-main = QA on the running dev server')}
            </Box>
          ))}
        </Box>
      )
    case 9: {
      // B-5: no Apply button here. The prefs are saved at step 10 with everything else (writeAll calls
      // applyPrefs), so "nothing is written until the final confirm" is true for all four of them too.
      const poll = pollValue(d.pollMin)
      return (
        <Box flexDirection="column" gap={1}>
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Pane')}
              {YesNo(els, d, act, 'autoOpen', 'Auto-open (fullscreen, ≥144 cols)')}
              {YesNo(els, d, act, 'chip', 'Status chip')}
              {YesNo(els, d, act, 'toasts', 'Toasts')}
            </Box>
          ))}
          {Card(els, poll.ok ? {} : { role: 'warn' }, (
            <Box flexDirection="column">
              {Hd(els, 'Usage poll')}
              {TextField(els, d, act, 'pollMin', 'Usage poll (minutes)')}
              {!poll.ok && Dim(els, `written as 5: "${d.pollMin}" is not a whole number of minutes ≥ 1`)}
            </Box>
          ))}
          {Card(els, {}, (
            <Box flexDirection="column">
              {Hd(els, 'Plugin settings today')}
              <Text>{`autoOpen ${options.autoOpen !== false ? 'on' : 'off'}, chip ${options.chip !== false ? 'on' : 'off'}, toasts ${options.toasts !== false ? 'on' : 'off'}, poll ${String(poll.value)} min. These live in the plugin's own settings (claude plugin configure matrixkoder), not in the yaml.`}</Text>
              {Dim(els, 'Saved with everything else at step 10 · Review lists them')}
            </Box>
          ))}
          {d.error != null && <Text color={ROLE.err.fg}>{`✗ ${d.error}`}</Text>}
        </Box>
      )
    }
    default:
      return Review(els, d, act, oldYaml, checkedAt)
  }
}
