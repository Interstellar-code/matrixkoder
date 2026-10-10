// The Settings tab's mock world (plan settings-tab S2.2): every row copied
// from the Settings.dc.html artboard, reshaped exactly like contract C1's
// `list --json` fields — {key, scope, label, type, value, default, min, max,
// unit, opts: [value, note], help, editable, why} — plus the C2 userConfig
// rows (mode, defaultTab, hiddenTabs, raster) in the same field shape with
// scope 'mode'/'tabs'. S3 swaps this object for mk-config's live answer
// without touching the render; values are fixture constants (the artboard's
// where it had them, brief schema-faithful helps where it had none).
//
// The plan's bound departures from the artboard, reflected here: no Hybrid
// mode (Solo / Orchestrator / Hive only); Plugin + Skills + Update collapse
// into ONE read-only About scope (scope 'about': the plugin options and
// helper probes read-only, editable: false); Models carries no ranks or
// scores (a dim cost note per model); `list` rows (repo.lanes.copy,
// builder_order, usage.providers) hold readonly string[] values the tab joins
// with ', '.
import type { SettingsView } from './settings'

/** the artboard's model list (its MODELS), the subagent-model select's options. */
const SUBAGENT_MODELS = [
  ['ollama-cloud/minimax-m3', 'paid'],
  ['opencode/step-5-preview-free', 'free'],
  ['opencode/space-bunny-free', 'free'],
  ['opencode-go/kimi-k3', '41% 5h'],
  ['zai-coding-plan/glm-5.1', '⊘ dropped'],
] as const

export const SETTINGS_FIXTURE: SettingsView = {
  mock: true,
  scopes: [
    { id: 'mode', label: 'Mode', file: '~/.matrixkoder/config.json', note: 'all projects' },
    { id: 'project', label: 'Project', file: '.matrixkoder/matrixkoder.yaml', note: 'this repo' },
    { id: 'routing', label: 'Routing', file: 'matrixkoder.yaml', note: 'routing_preferences' },
    { id: 'agents', label: 'Agents', file: 'matrixkoder.yaml', note: 'agents · usage' },
    { id: 'models', label: 'Models', file: 'yaml agents.*.models', note: 'max 4 per agent' },
    { id: 'child', label: 'Child', file: 'yaml lanes.child', note: 'lane sessions' },
    { id: 'tabs', label: 'Tabs', file: 'userConfig', note: 'dashboard' },
    { id: 'about', label: 'About', file: 'plugin.json', note: 'read-only' },
  ],
  groups: [
    // ---- Mode: the C2 `mode` row; the tab draws it as the mode cards, never
    // as a plain seg row (the group carries the field for S3's flat list)
    {
      scope: 'mode', title: 'operating mode', right: 'radio',
      rows: [
        {
          key: 'mode', scope: 'mode', label: 'Operating mode', type: 'seg',
          value: 'orchestrator', default: 'orchestrator',
          opts: [['solo', 'one session'], ['orchestrator', 'one repo'], ['hive', 'many repos']],
          help: 'Who builds: this session, one repo, or many.', editable: true,
        },
      ],
    },
    // ---- Project
    {
      scope: 'project', title: 'lanes', right: 'repo.lanes',
      rows: [
        { key: 'repo.lanes.slots', scope: 'project', label: 'Worktree slots', type: 'num', value: 3, default: 3, min: 1, max: 6, help: 'Reusable worktrees ../matrixkoder-mk-N.', editable: true },
        { key: 'repo.lanes.reuse', scope: 'project', label: 'Reuse slots', type: 'bool', value: true, default: true, help: 'Keep a slot after release.', editable: true },
        { key: 'repo.lanes.copy', scope: 'project', label: 'Copy into slot', type: 'list', value: ['.claude-plugin/types'], default: ['.claude-plugin/types'], help: 'Untracked paths a fresh slot needs.', editable: true },
        { key: 'repo.integration_branch', scope: 'project', label: 'Integration branch', type: 'text', value: 'main', default: 'main', help: 'Lanes merge here.', editable: true },
      ],
    },
    {
      scope: 'project', title: 'qa & autonomy',
      rows: [
        { key: 'qa.integration_target', scope: 'project', label: 'Merge target', type: 'seg', value: 'local-main', default: 'local-main', opts: [['local-main', 'local'], ['integration', 'integ']], help: 'Where reviewed lanes merge before QA.', editable: true },
        { key: 'autonomy_contract.builder_skip_permissions', scope: 'project', label: 'Builders skip perms', type: 'bool', value: true, default: true, help: 'Only inside their own worktree.', editable: true },
      ],
    },
    // ---- Routing
    {
      scope: 'routing', title: 'policy', right: 'routing_preferences',
      rows: [
        { key: 'routing_preferences.policy', scope: 'routing', label: 'Policy', type: 'seg', value: 'claude-sparing', default: 'claude-sparing', opts: [['claude-sparing', 'spare'], ['balanced', 'bal'], ['quality', 'qual']], help: 'Claude orchestrates; builders below.', editable: true },
        { key: 'routing_preferences.builder_order', scope: 'routing', label: 'Builder order', type: 'list', value: ['opencode', 'claude'], default: ['opencode', 'claude'], help: 'Codex never (user rule).', editable: true },
        { key: 'routing_preferences.max_build_lanes', scope: 'routing', label: 'Max build lanes', type: 'num', value: 3, default: 3, min: 1, max: 6, help: 'Build lanes the router may open at once.', editable: true },
      ],
    },
    {
      scope: 'routing', title: 'reserve floor', right: '% left',
      rows: [
        { key: 'reserve_floor.claude.five_hour_left_pct', scope: 'routing', label: 'Claude 5h', type: 'num', value: 40, default: 40, min: 0, max: 90, unit: '%', help: 'Below this Claude is not a builder.', editable: true },
        { key: 'reserve_floor.claude.weekly_left_pct', scope: 'routing', label: 'Claude week', type: 'num', value: 30, default: 30, min: 0, max: 90, unit: '%', help: 'Claude weekly floor.', editable: true },
        { key: 'reserve_floor.others.five_hour_left_pct', scope: 'routing', label: 'Others 5h', type: 'num', value: 10, default: 10, min: 0, max: 90, unit: '%', help: 'Five-hour floor for other agents.', editable: true },
        { key: 'reserve_floor.others.weekly_left_pct', scope: 'routing', label: 'Others week', type: 'num', value: 15, default: 15, min: 0, max: 90, unit: '%', help: 'Weekly floor for other agents.', editable: true },
      ],
    },
    {
      scope: 'routing', title: 'review',
      rows: [
        { key: 'routing_preferences.reviewer', scope: 'routing', label: 'Code reviewer', type: 'select', value: 'claude sonnet medium', default: 'claude sonnet medium', opts: [['claude sonnet medium', 'pane'], ['opencode-go/kimi-k3', 'pane'], ['claude opus high', 'data-loss']], help: 'New pane in the lane.', editable: true },
        { key: 'routing_preferences.visual_reviewer', scope: 'routing', label: 'Visual reviewer', type: 'select', value: 'claude haiku', default: 'claude haiku', opts: [['claude haiku', 'cheap'], ['claude sonnet', '']], help: 'PNG vs canvas checks.', editable: true },
      ],
    },
    // ---- Agents
    {
      scope: 'agents', title: 'builders', right: 'agents.*.available',
      rows: [
        { key: 'agents.opencode.available', scope: 'agents', label: 'OpenCode', type: 'bool', value: true, default: true, help: '12 models at bootstrap.', editable: true },
        { key: 'agents.claude.available', scope: 'agents', label: 'Claude Code', type: 'bool', value: true, default: true, help: 'Shares this pane’s subscription.', editable: true },
        { key: 'agents.herdr.available', scope: 'agents', label: 'herdr', type: 'bool', value: true, default: true, help: 'Panes and worktrees, not a builder.', editable: true },
        { key: 'agents.codex.available', scope: 'agents', label: 'Codex', type: 'bool', value: false, default: false, help: 'Never routed (user rule).', editable: true },
        { key: 'agents.agy.available', scope: 'agents', label: 'AGY', type: 'bool', value: false, default: false, help: 'Gemini models via the agy CLI.', editable: true },
      ],
    },
    {
      scope: 'agents', title: 'usage', right: 'usage',
      rows: [
        { key: 'usage.source', scope: 'agents', label: 'Source', type: 'seg', value: 'codexbar', default: 'codexbar', opts: [['codexbar', 'codexbar'], ['manual', 'manual']], help: 'Where usage numbers come from.', editable: true },
        { key: 'usage.poll_min', scope: 'agents', label: 'Poll', type: 'num', value: 5, default: 5, min: 1, max: 60, unit: 'm', help: 'Minutes between usage polls.', editable: true },
        { key: 'usage.providers', scope: 'agents', label: 'Providers', type: 'list', value: ['claude', 'opencodego', 'ollama'], default: ['claude', 'opencodego', 'ollama'], help: 'Z.AI dropped 2026-10-08.', editable: true },
      ],
    },
    // ---- Child
    {
      scope: 'child', title: 'builder sessions', right: 'lanes.child',
      rows: [
        { key: 'lanes.child.fresh_session', scope: 'child', label: 'New round', type: 'seg', value: '/new', default: '/new', opts: [['/new', '/new'], ['same', 'same']], help: 'Clear context per round; model stays.', editable: true },
        { key: 'lanes.child.subagents', scope: 'child', label: 'Subagents', type: 'bool', value: true, default: true, help: 'Plan default (template).', editable: true },
        { key: 'lanes.child.subagent_model', scope: 'child', label: 'Subagent model', type: 'select', value: 'ollama-cloud/minimax-m3', default: 'ollama-cloud/minimax-m3', opts: SUBAGENT_MODELS, help: 'Written to the lane oh-my-openagent categories.', editable: true },
        { key: 'lanes.child.ctx_stop_pct', scope: 'child', label: 'Stop at context', type: 'num', value: 80, default: 80, min: 50, max: 95, unit: '%', help: 'Interrupt + takeover in a fresh session.', editable: true },
        { key: 'lanes.child.cost_stop_usd', scope: 'child', label: 'Stop at cost', type: 'num', value: 5, default: 5, min: 1, max: 50, help: 'Per round, paid models.', editable: true },
      ],
    },
    {
      scope: 'child', title: 'watch', right: 'lane-watch',
      rows: [
        { key: 'lanes.child.stall_min', scope: 'child', label: 'Stall after', type: 'num', value: 20, default: 20, min: 5, max: 60, unit: 'm', help: 'No file/commit/pane change.', editable: true },
        { key: 'lanes.child.max_wait_min', scope: 'child', label: 'Give up after', type: 'num', value: 120, default: 120, min: 30, max: 300, unit: 'm', help: 'Give up on a silent lane after this.', editable: true },
        { key: 'herdr.events', scope: 'child', label: 'herdr events plugin', type: 'bool', value: true, default: true, help: 'herdr-plugin.toml events.', editable: true },
      ],
    },
    {
      scope: 'child', title: 'reviewer panes',
      rows: [
        { key: 'lanes.child.reviewer_split', scope: 'child', label: 'Split', type: 'seg', value: 'right', default: 'right', opts: [['right', 'right'], ['down', 'down']], help: 'herdr pane split.', editable: true },
        { key: 'lanes.child.reuse_reviewer', scope: 'child', label: 'Reuse reviewer pane', type: 'bool', value: true, default: true, help: '/clear between rounds.', editable: true },
      ],
    },
    // ---- Tabs: the artboard's visibility + pane rows, plus the C2 rows
    // (defaultTab, hiddenTabs, raster) in the same field shape
    {
      scope: 'tabs', title: 'visible tabs', right: 'dashboard.tabs',
      rows: [
        { key: 'tabs.overview', scope: 'tabs', label: 'o Overview', type: 'bool', value: true, default: true, help: 'The run at a glance.', editable: true },
        { key: 'tabs.lanes', scope: 'tabs', label: 'l Lanes', type: 'bool', value: true, default: true, help: 'One row per builder lane.', editable: true },
        { key: 'tabs.plan', scope: 'tabs', label: 'p Plan', type: 'bool', value: true, default: true, help: 'Waves and phases.', editable: true },
        { key: 'tabs.usage', scope: 'tabs', label: 'u Usage', type: 'bool', value: true, default: true, help: 'Quota bars per agent.', editable: true },
        { key: 'tabs.events', scope: 'tabs', label: 'e Events', type: 'bool', value: true, default: true, help: 'The run’s event log.', editable: true },
        { key: 'tabs.waiting', scope: 'tabs', label: 'w Waiting', type: 'bool', value: true, default: true, help: 'Decisions open on you.', editable: true },
        { key: 'tabs.old', scope: 'tabs', label: '0 Old', type: 'bool', value: true, default: true, help: 'Retired later.', editable: true },
      ],
    },
    {
      scope: 'tabs', title: 'pane', right: 'dashboard',
      rows: [
        { key: 'defaultTab', scope: 'tabs', label: 'Open on', type: 'select', value: 'overview', default: 'overview', opts: [['overview', ''], ['lanes', ''], ['plan', ''], ['usage', ''], ['events', ''], ['waiting', ''], ['old', ''], ['settings', 'this tab']], help: 'The tab the pane opens on.', editable: true },
        { key: 'hiddenTabs', scope: 'tabs', label: 'Hidden tabs', type: 'text', value: '', default: '', help: 'Comma list of tabs to hide (Settings never hidden).', editable: true },
        { key: 'dashboard.auto_open', scope: 'tabs', label: 'Auto-open pane', type: 'bool', value: true, default: true, help: '≥144 cols, fullscreen.', editable: true },
        { key: 'dashboard.chip', scope: 'tabs', label: 'Status chip', type: 'bool', value: true, default: true, help: 'Run health under the prompt.', editable: true },
        { key: 'dashboard.toasts', scope: 'tabs', label: 'Toasts', type: 'bool', value: true, default: true, help: 'Toast on task status transitions.', editable: true },
        { key: 'raster', scope: 'tabs', label: 'Raster bars', type: 'bool', value: true, default: true, help: 'Off = eighth-block fallback.', editable: true },
      ],
    },
    // ---- About: read-only (the plan's merged Plugin + Skills + Update)
    {
      scope: 'about', title: 'plugin options', right: 'userConfig',
      rows: [
        { key: 'wake', scope: 'about', label: 'Wake on events', type: 'bool', value: true, default: true, help: 'Submit a prompt when a lane needs attention.', editable: false, why: 'Read-only: claude plugin configure' },
        { key: 'guards', scope: 'about', label: 'Guards', type: 'bool', value: true, default: true, help: 'Deny destructive git, banned commands.', editable: false, why: 'Read-only: claude plugin configure' },
        { key: 'usagePollMin', scope: 'about', label: 'Usage poll', type: 'num', value: 5, default: 5, min: 1, max: 60, unit: 'm', help: 'Minutes between usage polls.', editable: false, why: 'Read-only: claude plugin configure' },
        { key: 'autoOpen', scope: 'about', label: 'Auto-open pane', type: 'bool', value: true, default: true, help: 'Open the pane when a session starts.', editable: false, why: 'Read-only: claude plugin configure' },
        { key: 'chip', scope: 'about', label: 'Status chip', type: 'bool', value: true, default: true, help: 'Run health chip under the prompt.', editable: false, why: 'Read-only: claude plugin configure' },
        { key: 'toasts', scope: 'about', label: 'Toasts', type: 'bool', value: true, default: true, help: 'Toast on task status transitions.', editable: false, why: 'Read-only: claude plugin configure' },
      ],
    },
    {
      scope: 'about', title: 'helpers', right: 'probe result',
      rows: [
        { key: 'helpers.herdr', scope: 'about', label: 'herdr', type: 'seg', value: 'ok', default: 'ok', opts: [['ok', 'ok'], ['off', 'off']], help: 'Pane and worktree manager.', editable: false, why: 'Read-only: probed at preflight' },
        { key: 'helpers.jev', scope: 'about', label: 'Jev', type: 'seg', value: 'ok', default: 'ok', opts: [['ok', 'ok'], ['off', 'off']], help: 'Judgement gates for routing.', editable: false, why: 'Read-only: probed at preflight' },
        { key: 'helpers.context_mode', scope: 'about', label: 'context-mode', type: 'seg', value: 'ok', default: 'ok', opts: [['ok', 'ok'], ['off', 'off']], help: 'Context compression for big outputs.', editable: false, why: 'Read-only: probed at preflight' },
        { key: 'helpers.codebase_memory', scope: 'about', label: 'codebase-memory', type: 'seg', value: 'missing', default: 'missing', opts: [['ok', 'ok'], ['off', 'off'], ['missing', 'miss']], help: 'Serena-style project memory.', editable: false, why: 'Read-only: probed at preflight' },
        { key: 'helpers.canary', scope: 'about', label: 'canary', type: 'seg', value: 'ok', default: 'ok', opts: [['ok', 'ok'], ['off', 'off']], help: 'Browser QA recordings.', editable: false, why: 'Read-only: probed at preflight' },
      ],
    },
  ],
  // the three bound modes (no Hybrid — the plan's departure)
  modes: [
    { id: 'solo', name: 'Solo', scope: 'one session', desc: 'MK without orchestration: dashboard, usage, guards, state. You build here.' },
    { id: 'orchestrator', name: 'Orchestrator', scope: 'one repo', desc: 'Full MK loop on this repo: plan, lanes, builders, reviews, merge.' },
    { id: 'hive', name: 'Hive', scope: 'many repos', desc: 'One MK coordinates several repos: shared quota, lanes and runs.' },
  ],
  projects: [
    { name: 'matrixkoder', since: '10-08', on: true },
    { name: 'hermes-switchui', since: '10-07', on: true },
    { name: 'hermes-desktop', since: '10-08', on: false },
  ],
  // the artboard's catalogue: names + dim cost notes, no ranks or scores
  agents: [
    {
      id: 'opencode', name: 'OpenCode', provider: 'opencode · opencode-go · ollama-cloud', off: false,
      models: [
        { name: 'ollama-cloud/minimax-m3', note: 'paid' },
        { name: 'opencode-go/kimi-k3', note: '$' },
        { name: 'opencode/step-5-preview-free', note: 'free' },
        { name: 'opencode/space-bunny-free', note: 'free' },
        { name: 'opencode-go/claude-haiku-5-5', note: '$' },
        { name: 'opencode/longcat-2.5-preview-free', note: 'free' },
        { name: 'opencode/nemotron-3-ultra-free', note: 'free' },
        { name: 'ollama-cloud/minimax-m2.7', note: '$' },
      ],
    },
    {
      id: 'claude', name: 'Claude Code', provider: 'anthropic subscription', off: false,
      models: [
        { name: 'claude-fable-5-1', note: 'sub' },
        { name: 'claude-opus-5-5', note: 'sub' },
        { name: 'claude-sonnet-5-5', note: 'sub' },
        { name: 'claude-haiku-5-5', note: 'sub' },
      ],
    },
    {
      id: 'agy', name: 'AGY', provider: 'google', off: true,
      models: [
        { name: 'gemini-3.1-pro-high', note: 'sub' },
        { name: 'gemini-3.8-flash-high', note: 'sub' },
        { name: 'gemini-3.8-flash-medium', note: 'sub' },
        { name: 'gemini-3.7-flash-low', note: 'sub' },
      ],
    },
    {
      id: 'codex', name: 'Codex', provider: 'openai', off: true,
      models: [
        { name: 'gpt-6.1-sol high', note: 'sub' },
        { name: 'gpt-6.1-mini', note: 'sub' },
      ],
    },
  ],
  // the artboard's PICKS0: ordered, max 4 per agent
  picks: [
    { agent: 'opencode', models: ['ollama-cloud/minimax-m3', 'opencode/step-5-preview-free', 'opencode/space-bunny-free'] },
    { agent: 'claude', models: ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5-5', 'claude-fable-5-1'] },
    { agent: 'agy', models: [] },
    { agent: 'codex', models: [] },
  ],
  about: {
    installed: '0.2.0',
    latest: '0.3.0',
    latestNote: 'checked 10-09',
    commits: 18,
    whatsNew: [
      { kind: 'feat', line: 'dashboard visual kit, bordered tabs' },
      { kind: 'feat', line: 'mk-state fan-out, aliases, verify' },
      { kind: 'fix', line: 'wake floods, reviewer dispatch' },
    ],
  },
}
