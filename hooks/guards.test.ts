// Guards tests (spikes Q11 pattern): the plugin under test is loaded by
// `claude plugin test`; each test answers Bash beneath it so allowed calls
// never reach a real shell and denied ones prove the guard fired.
import { test, expect } from 'claude-code/testing'
import type { On } from 'claude-code'

const stubBash = (on: On) =>
  on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: '', stderr: '', interrupted: false } }))

// Session cwd /proj/app/sub; the nearest `.matrixkoder/` is /proj/app's, which holds `yaml`.
const project = (on: On, yaml: string) => {
  on('session.cwd', () => ({ value: '/proj/app/sub' }))
  on('fs.stat', (_$, e, next) =>
    e.path === '/proj/app/.matrixkoder' ? { value: { kind: 'dir', size: 0, mtimeMs: 0, isLink: false } } : next(e),
  )
  on('fs.read', (_$, e, next) => (e.path === '/proj/app/.matrixkoder/matrixkoder.yaml' ? { value: yaml } : next(e)))
}

// The commands of `list` the guard did NOT deny (deny = true) / DID deny (deny = false).
type Call = (c: { tool: 'Bash'; command: string }) => Promise<{ deny?: string }>
async function misses(call: Call, list: readonly string[], deny: boolean): Promise<string[]> {
  const out: string[] = []
  for (const command of list) {
    const ran = await call({ tool: 'Bash', command })
    if (deny !== (ran.deny?.startsWith('MatrixKoder guard: ') ?? false)) out.push(command)
  }
  return out
}

// Denied by the first guard already.
const DENY_BASIC = [
  'git checkout main',
  'git checkout -b lane/x',
  'git stash',
  'git stash pop',
  'git reset --hard HEAD~1',
  'git clean -fd',
  'git restore src/app.ts',
  'git restore --source=HEAD~1 src/app.ts',
  'git restore --staged --worktree f',
  'git restore -SW f',
  'git restore -W --staged f',
  'git commit --amend --no-edit',
  'git commit -m "fix" --amend',
  'git push --force origin main',
  'git push -f origin main',
  'git push --force-with-lease',
  'cd /tmp && git stash',
  'git log; git clean -fd',
  'echo hi | git reset --hard',
  'true || git checkout main',
  'git status\ngit restore .',
]

// Review G-Bfix1 forms: each one was ALLOWED by 07fb8ca.
const DENY_REVIEW = [
  // git global options before the subcommand, path to git
  'git -C ../wt checkout main',
  'git -c core.pager=cat reset --hard',
  'git --no-pager checkout x',
  'git --git-dir=.git --work-tree=. stash',
  'git --git-dir .git reset --hard',
  'git --work-tree . clean -fd',
  '/usr/bin/git reset --hard',
  './git checkout x',
  // prefixes
  'X=1 git reset',
  'env X=1 git clean -fd',
  'env -i PATH=/bin git stash',
  'command git stash',
  'sudo git restore f',
  'sudo -u root git checkout main',
  'nice git reset --hard',
  'nice -n 5 git stash',
  'time git checkout x',
  'xargs git checkout',
  'nohup git reset --hard',
  'exec git clean -fd',
  // nesting
  'echo $(git checkout x)',
  'echo `git reset`',
  '(git reset --hard)',
  '{ git reset; }',
  'echo "$(git stash)"',
  'echo "`git clean -fd`"',
  'bash -c "git reset --hard"',
  "sh -c 'git checkout x'",
  'zsh -c "git stash"',
  "bash -lc 'cd x && git restore .'",
  'eval "git reset --hard"',
  'xargs sh -c "git stash"',
  `bash -c "bash -c \\"bash -c 'git reset'\\""`,
  // separators
  'sleep 1 & git reset --hard',
  'echo hi |& git checkout x',
  // force push
  'git push -fu origin main',
  'git push origin +main',
  'git push origin +HEAD:refs/heads/main',
  'git push --force-with-lease=main:abc origin main',
  'git -C wt push -f',
  // switch
  'git switch -f main',
  'git switch --discard-changes main',
  'git switch --force main',
  // quoted / escaped subcommand, abbreviated --amend
  'git "checkout" x',
  "git 'reset' --hard",
  'git \\reset',
  'git commit --amen',
]

const ALLOW = [
  'git log --oneline -5',
  'git status',
  'git diff HEAD~1',
  'git worktree add ../lane -b lane/x',
  'git push origin main',
  'git push -u origin main',
  'git switch main',
  'git switch -c feat',
  'git -C ../wt status',
  'env X=1 git status',
  'command -v git',
  'git log 2>&1 | head',
  'git restore --staged f',
  'git restore -S f',
  'git restore --staged -- a b',
  'python3 scripts/mk-state.py run list',
  // quoted mentions
  'echo "git reset"',
  'echo "git stash would be bad"',
  'git commit -m "do not git reset"',
  'git commit -m "fix git stash bug"',
  "grep 'git checkout' f",
  'echo "x && git stash"',
  'git commit -m "a; git reset"',
  "echo 'a | git checkout x'",
  "echo '$(git reset)'",
  'echo "a \\"; git reset\\""',
  "git commit -m '--amend'",
  'echo hi # git reset',
  "bash -c 'git status'",
]

test('denies the banned git forms the first guard caught', async ($, on) => {
  stubBash(on)
  expect(await misses(c => $.tool.call(c), DENY_BASIC, true)).toEqual([])
  const ran = await $.tool.call({ tool: 'Bash', command: 'git stash' })
  expect(ran.deny).toBe('MatrixKoder guard: git stash is banned (destructive git undo). Undo by an inverse edit instead.')
})

test('denies the review bypass forms', async ($, on) => {
  stubBash(on)
  expect(await misses(c => $.tool.call(c), DENY_REVIEW, true)).toEqual([])
})

test('allows safe git, restore --staged and quoted mentions', async ($, on) => {
  stubBash(on)
  expect(await misses(c => $.tool.call(c), ALLOW, false)).toEqual([])
})

test('denies banned_commands from the nearest .matrixkoder/matrixkoder.yaml', async ($, on) => {
  stubBash(on)
  project(
    on,
    [
      'gates:',
      '  - test',
      'banned_commands:',
      '  - "terraform apply"',
      '',
      '  # deploys go through CI',
      '  - mk-state.py archive   # orchestrator only',
      "  - 'rm -rf'",
      'other: 1',
      '  - not-banned',
    ].join('\n'),
  )
  const denied = await misses(
    c => $.tool.call(c),
    [
      'terraform apply -auto-approve',
      'mk-state.py archive --id r1 && echo done',
      'rm -rf /tmp/x',
      'rm -fr /tmp/x',
      '/bin/rm -rf x',
      'sudo terraform apply',
      'env A=1 terraform apply',
    ],
    true,
  )
  expect(denied).toEqual([])
  const hit = await $.tool.call({ tool: 'Bash', command: 'terraform apply' })
  expect(hit.deny).toContain('MatrixKoder guard: terraform apply is banned (banned in matrixkoder.yaml)')
  expect(await misses(c => $.tool.call(c), ['terraform plan', 'not-banned', 'echo "rm -rf"'], false)).toEqual([])
})

test('reads a flow-list banned_commands', async ($, on) => {
  stubBash(on)
  project(on, 'banned_commands: ["terraform apply", \'make deploy\']  # inline\n')
  expect(await misses(c => $.tool.call(c), ['terraform apply', 'make deploy'], true)).toEqual([])
  expect(await misses(c => $.tool.call(c), ['make test'], false)).toEqual([])
})

test('registers nothing when userConfig guards is false', { options: { guards: false } }, async ($, on) => {
  stubBash(on)
  const ran = await $.tool.call({ tool: 'Bash', command: 'git stash' })
  expect(ran.deny).toBeUndefined()
})
