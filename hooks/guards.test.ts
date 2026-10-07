// Guards tests (spikes Q11 pattern): the plugin under test is loaded by
// `claude plugin test`; each test answers Bash beneath it so allowed calls
// never reach a real shell and denied ones prove the guard fired.
import { test, expect } from 'claude-code/testing'
import type { On, Plugin } from 'claude-code'

const stubBash = (on: On) =>
  on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: '', stderr: '', interrupted: false } }))

// an inline plugin may use $ freely where a test's hooks may not: on its first
// Bash call it writes the banned_commands yaml (via $.fs.write) ahead of the guard
const writesYaml: Plugin = {
  name: 'writes-yaml',
  tier: 'prepend',
  register(on, _options) {
    return on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
      if (!(await $.fs.read('.matrixkoder/matrixkoder.yaml').catch(() => ''))) {
        await $.fs.write(
          '.matrixkoder/matrixkoder.yaml',
          'gates:\n  - test\nbanned_commands:\n  - "terraform apply"\n  - mk-state.py archive\n',
        )
      }
      return next(e)
    })
  },
}

test('denies every banned git form', async ($, on) => {
  stubBash(on)
  for (const command of [
    'git checkout main',
    'git checkout -b lane/x',
    'git stash',
    'git stash pop',
    'git reset --hard HEAD~1',
    'git clean -fd',
    'git restore src/app.ts',
    'git commit --amend --no-edit',
    'git commit -m "fix" --amend',
    'git push --force origin main',
    'git push -f origin main',
    'git push --force-with-lease',
  ]) {
    const ran = await $.tool.call({ tool: 'Bash', command })
    expect(ran.deny, command).toContain('MatrixKoder guard: git')
    expect(ran.deny, command).toContain('Undo by an inverse edit instead.')
  }
})

test('allows safe git, quoted mentions and non-git commands', async ($, on) => {
  stubBash(on)
  for (const command of [
    'git log --oneline -5',
    'git status',
    'git diff HEAD~1',
    'git worktree add ../lane -b lane/x',
    'git commit -m "do not git reset"',
    'git push origin main',
    'echo "git stash would be bad"',
    'python3 scripts/mk-state.py run list',
  ]) {
    const ran = await $.tool.call({ tool: 'Bash', command })
    expect(ran.deny, command).toBeUndefined()
  }
})

test('denies banned forms hidden after ; && || | and newlines', async ($, on) => {
  stubBash(on)
  for (const command of [
    'cd /tmp && git stash',
    'git log; git clean -fd',
    'echo hi | git reset --hard',
    'true || git checkout main',
    'git status\ngit restore .',
  ]) {
    const ran = await $.tool.call({ tool: 'Bash', command })
    expect(ran.deny, command).toContain('MatrixKoder guard: git')
  }
})

test('denies banned_commands from .matrixkoder/matrixkoder.yaml', { plugins: [writesYaml] }, async ($, on) => {
  stubBash(on)
  const files: Record<string, string> = {}
  on('fs.write', (_$, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.read', (_$, e, next) => (e.path in files ? { value: files[e.path] } : next(e))) // miss rejects like a missing file
  const hit = await $.tool.call({ tool: 'Bash', command: 'terraform apply -auto-approve' })
  expect(hit.deny).toContain('MatrixKoder guard: terraform apply is banned (banned in matrixkoder.yaml)')
  const hit2 = await $.tool.call({ tool: 'Bash', command: 'mk-state.py archive --id r1 && echo done' })
  expect(hit2.deny).toContain('mk-state.py archive')
  const miss = await $.tool.call({ tool: 'Bash', command: 'terraform plan' }) // prefix, not the banned command
  expect(miss.deny).toBeUndefined()
})

test('registers nothing when userConfig guards is false', { options: { guards: false } }, async ($, on) => {
  stubBash(on)
  const ran = await $.tool.call({ tool: 'Bash', command: 'git stash' })
  expect(ran.deny).toBeUndefined()
})
