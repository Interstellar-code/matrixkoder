# Why omp helpers return without writing files (todo 97)

Evidence-based diagnosis from real omp session logs under `~/.omp/agent/sessions/`
(2026-10-14). Every claim below cites a log/config line. Anything inferred is marked
**not verified**.

Paths below are relative to `~/.omp/agent/sessions/`. Redaction: no API key from
`~/.omp/agent/models.yml` is quoted here.

## Verdict

**Cause: the helper ran on `manifest/auto` (the `auto` router) and ended its turn
early — a text-only `stop` with no tool call — before it made a single `edit`/`write`
call.** Not a permission problem, not a missing worktree path, not a truncated response.

Every helper in this run that resolved to `manifest/auto` wrote **zero** files
(7 of 7, across two lanes and four spawn sites). Every helper that resolved to
`opencode-go/step-5-preview-free` wrote files (3 of 3). The split is perfect on this
sample.

| helper | session log | `resolvedModel` | `agent` | `readOnly` | edit/write calls |
|---|---|---|---|---|---|
| O21 | `…-mk-4/2026-10-14T12-30-55-807Z_…/O21.jsonl:4` | `manifest/auto` | `task` | `false` | 0 |
| O22 | same dir, `O22.jsonl:4` | `manifest/auto` | `task` | `false` | 0 |
| OldCodeCheck | `…-mk-3/2026-10-14T11-18-35-664Z_…/OldCodeCheck.jsonl:4` | `manifest/auto` | `task` | `false` | 0 |
| CatalogProbe | same dir, `CatalogProbe.jsonl:4` | `manifest/auto` | `task` | `false` | 0 |
| ConfigKeys | same dir, `ConfigKeys.jsonl:4` | `manifest/auto` | `task` | `false` | 0 |
| PlanArchitect | `-Development-matrixkoder/2026-10-14T10-34-26-754Z_…/PlanArchitect.jsonl:4` | `manifest/auto` | `effort-high` | `false` | 0 |
| PlanCritic | same dir, `PlanCritic.jsonl:4` | `manifest/auto` | `effort-high` | `false` | 0 |
| **L0State** | `…-mk-3/2026-10-14T14-54-55-010Z_…/L0State.jsonl:4` | `opencode-go/step-5-preview-free` | `effort-high` | `false` | **84** |
| **L0Bus** | same dir, `L0Bus.jsonl:4` | `opencode-go/step-5-preview-free` | `effort-high` | `false` | **52** |
| L0ToolsWake | same dir, `L0ToolsWake.jsonl:4` | `opencode-go/step-5-preview-free` | `effort-high` | `false` | 3 |
| ProbeHarness | same dir, `ProbeHarness.jsonl:4` | `opencode-go/step-5-preview-free` | `effort-high` | `false` | 0 (read-only task; expected) |
| Scout* / BaselineCheck (6) | same dir, `Scout*.jsonl:4` | `ollama-cloud/deepseek-v4.1-flash` | `scout` | — | 0 (read-only agent; expected) |

The two `session_init` lines are the proof that the toolset and the write flag were
identical on both sides. `O21.jsonl:4` (abridged, one line):

```
"tools":["read","bash","edit","eval","glob","grep","task","wait","web_search","write",
 "ast_grep","ast_edit","debug","github","memory_edit","retain","recall","reflect"],
"agent":"task","resolvedModel":"manifest/auto","readOnly":false,"spawns":"*"
```

`L0State.jsonl:4` carries the same `tools` array, `"readOnly":false`, and
`"resolvedModel":"opencode-go/step-5-preview-free"`. So **write permission is ruled
out**: the failing helpers held `write` and `edit` in their tool list with
`readOnly:false`, exactly like the ones that wrote.

## The failing turn, quoted

O21's last assistant record (`…/O21.jsonl:36`) has `stopReason:"stop"`, 3751 output
tokens, `compactionEpoch:0`, and a single `text` part — no `toolCall`:

```
"The first acquire assertion is an exact dict match, so the new field has to land
 there too. I'll update the install, exclude list, and self-test together."
```

O22 is the same shape (`O22.jsonl:41`, 589 output tokens):

```
"The ChatGPT and missing-key lines aren't matched. I'll add the smallest patterns and
 a self-test fixture."
```

Both then exited normally, not by crash — `O21.jsonl:37` and `O22.jsonl:42`:

```
{"type":"custom","customType":"session_exit","data":{"reason":"dispose","kind":"normal",
 "recordedAt":"2026-10-14T12:44:16.484Z"}}
```

`reason:"dispose"` is the parent disposing a finished child, so the harness delivered
these as completed jobs. The lead's side of the story, from the lane session
`…-mk-4/2026-10-14T12-30-55-807Z_….jsonl`:

- `:150` — `<task-result id="O21" agent="task" status="completed" duration="1m40s">`
  carrying that same one-sentence text as the helper's output.
- `:152` — the lead ran `git diff --stat -- scripts/mk-lane.py && git diff -- scripts/mk-lane.py`;
  `:153` is the result: `(no output)`.
- `:155` — `git status --short && echo '---' && git diff --stat HEAD`; `:156` result: `---\n`
  (nothing before the separator, nothing after).
- `:165` — the lead's own summary: `"Helpers stopped before writing. I'll implement O2.1,
  the fresh-session command, and the quota lines myself."`

So the diff really was empty, and the helper really had claimed completion. That is
exactly the `mk-builder` failure mode R8 exists for
(`child-skills/mk-builder/reference/lane-rules.md:79-83`: "a subagent marked done that
wrote nothing is a failure").

## What the failing helpers did do

Both spent their whole budget reading and grepping, then stopped. Tool-call census of
the failing helpers, counted from their own logs:

| helper | tool calls (all) | edit/write |
|---|---|---|
| O21 | 11 (`grep` 5, `read` 6) | 0 |
| O22 | 12 (`eval` 6, `grep` 4, `read` 2) | 0 |
| CatalogProbe | 11 (`read` 9, `grep` 2) | 0 |
| ConfigKeys | 8 (`read` 4, `grep` 4) | 0 |

O21's last recorded intent before stopping was still reconnaissance: a `read` of
`scripts/mk-lane.py:1588-1630` (`O21.jsonl`, `tool_execution_start` at
`2026-10-14T12:36:40.783Z`). It never reached the edit it had just described.

## Turn-shape census (the discriminator)

| model | toolUse turns | text-only `stop` turns | edit/write calls |
|---|---|---|---|
| `manifest/auto` (`auto`) | 401 | 53 | **1** (a lead's report file — see below) |
| `opencode-go/step-5-preview-free` | 585 | 1 | 210 |
| `opencode-go/space-bunny` | 475 | 7 | 115 |
| `ollama-cloud/deepseek-v4.1-flash` | 228 | 10 | 17 |
| `ollama-cloud/mistral-large-4` | 97 | 4 | 11 |

Counted over every `*.jsonl` under `~/.omp/agent/sessions/` (helpers and leads).
`auto` is by far the most prose-happy route — 53 turns that end in text with no tool
call, against 1 for `step-5-preview-free` — and it is the only model whose writes are
effectively nil. No `auto` **helper** has ever emitted an `edit` or `write`.

The single `auto` write is not a counterexample to the helper finding: it is the O1
**lane lead**'s own report file, at
`…-mk-3/2026-10-14T11-18-35-664Z_….jsonl:420` (`2026-10-14T12:43:20.621Z`,
`write /Users/rohits/Development/matrixkoder-mk-3/.matrixkoder-report-r1.md`), after 90
`toolUse` turns on the same model — a `write` of new markdown at the very end of a
round, not an `edit` of owned source, and not a helper.

**Not verified:** that `manifest/auto` maps to a specific upstream model. `auto` is a
router id (`~/.omp/agent/models.yml:7`, `id: auto`, `name: Manifest Auto`,
`maxTokens: 65536`, `contextWindow: 500000`), and the response logs do not say which
model served a turn. The failure is attributable to the `manifest/auto` route, not to
a named model behind it.

## The contrary data point: the L0 lane worked

The storage-layout L0 lane in `…-mk-3/2026-10-14T14-54-55-010Z_…` spawned `L0State` and
`L0Bus` and its files changed:

- `L0State.jsonl:3` — `{"type":"model_change","model":"opencode-go/step-5-preview-free",
  "resolvedModelIsFallback":false}`; `L0State.jsonl:4` — `agent:"effort-high"`,
  `resolvedModel:"opencode-go/step-5-preview-free"`, `readOnly:false`.
- First `edit` at `L0State.jsonl:78` (`2026-10-14T15:30:09.355Z`, `scripts/mk-state.py`);
  84 `edit`/`write` calls in the session.
- `L0Bus.jsonl:134` — first `edit` at `2026-10-14T15:34:12.434Z` (`scripts/mk-bus.py`);
  52 in the session.

So the harness, the worktree, the toolset and `readOnly:false` all permit writes. The
only variable that moves with the outcome is the resolved model.

The lead lanes corroborate it from the other side. In `…-mk-4/2026-10-14T12-30-55-807Z_….jsonl`
the lead itself started on `manifest/auto` (`:3`) and made **no** `edit`/`write` call
for the first 21 minutes; at `:200`/`:201` it switched to `opencode-go/space-bunny` and
its first `edit` lands at `:205` (`2026-10-14T12:51:46.790Z`), then 28 writes. Same
pattern in `…-mk-3/2026-10-14T13-13-51-777Z_….jsonl`: `model_change` to
`step-5-preview-free` at `:95` (`13:30:32.694Z`), first `edit` at `:101`
(`13:30:43.612Z`), 19 writes. Same agent, same lane, same tools, same prompt — only the
model changed, and the writes started within seconds.

## Candidates, ranked

**1. Helper model = `manifest/auto`, and that route ends a turn before acting.** (cause)
For: 7/7 `auto` helpers wrote nothing; 3/3 `step-5` helpers wrote; `auto` totals 1 write
in 401 turns across the whole machine and that one is a lead's report file; the two
failing transcripts end in a prose-only `stop`; leads on `auto` also produced no source
writes until they switched model, then wrote within seconds. Against: the O1 lead's
report-file write on the same model (see above) shows `auto` is not incapable of
calling `write` at all — only that its helpers never reach an edit. Scope not verified
— 7 helper samples, one day, one provider.

**2. Early stop.** (mechanism, not an independent cause) The harness behaved correctly:
`session_exit` is `reason:"dispose"`, `kind:"normal"`, so a text-only `stop` is treated
as a completed job. R8 already tells the lane to check `git status` and redo the work,
which is what happened. The defect is upstream of the harness: a `stop` is not
distinguished from "done the work".

**3. Write permission.** Ruled out. `O21.jsonl:4` / `O22.jsonl:4` carry `write` and
`edit` in `tools` with `readOnly:false`, identical to the working helpers. No permission
or EACCES error appears in either transcript.

**4. Helper not given the worktree path.** Ruled out as the cause; it is a real
hygiene gap. Both failing prompts were given the target as a repo-relative path
(`O21` task text: `# Target\nscripts/mk-lane.py only.`), and both helpers then operated
on the correct absolute worktree paths under `/Users/rohits/Development/matrixkoder-mk-4/`
(e.g. O21's read of `scripts/mk-lane.py:1588-1630`, and its first grep of
`…/matrixkoder-mk-4/scripts/mk-lane.py`). The lane session's own `cwd` is the worktree
(`{"type":"session","cwd":"/Users/rohits/Development/matrixkoder-mk-4"}`). So the helper
knew where the worktree was; it stopped for another reason.

## Reproducible check the orchestrator can run

Read-only; no helper spawn, no writes. In any omp lane pane:

```
S=~/.omp/agent/sessions/-Development-matrixkoder-mk-4
# 1. which model did each helper resolve to?
for f in "$S"/*/*.jsonl; do
  printf '%s\t%s\t%s\n' "$(basename "$f")" \
    "$(jq -r 'select(.type=="session_init")|.resolvedModel' "$f" | head -1)" \
    "$(jq -r 'select(.type=="message")|.message|select(.role=="assistant")|[.content[]?|select(.type=="toolCall" and (.name=="edit" or .name=="write"))]|length' "$f" | awk '{s+=$1} END{print s+0}')"
done | sort -k2

# 2. did the helper end on a prose-only stop?
jq -rc 'select(.type=="message")|.message|select(.role=="assistant" and .stopReason=="stop")
  | select([.content[]?|select(.type=="toolCall")]|length==0)|.content[0].text' "$S"/*/*.jsonl | tail
```

Pass criterion for a fixed lane: every helper row shows a non-`manifest/auto`
`resolvedModel` and a non-zero write count, and step 2 prints nothing for helpers whose
task named files. Today it prints `manifest/auto … 0` seven times.

A second, model-only check without any lane work: switch one omp session's model away
from `manifest/auto` and watch for the first `edit`/`write` tool call — the mk-4 and
mk-3 leads both produced their first write within ~15 s of the switch
(`…mk-4…12-30-55…jsonl:200,205`; `…mk-3…13-13-51…jsonl:95,101`).

## Smallest proposed fix (not applied)

**Pin the omp lane's helper/lead model off `manifest/auto` for omp lanes, so helpers
inherit a model that acts.**

Exact file: `/Users/rohits/Development/matrixkoder/.matrixkoder/matrixkoder.yaml`, the
`picks:` block — line 75 currently reads `omp: [manifest/auto]` (line 35 also carries
`omp: { available: true, models: [manifest/auto] }`). Changing that one list to a model
already proven to write in this repo's lanes (`opencode-go/step-5-preview-free`, used by
the L0 helpers at `L0State.jsonl:4`) is the whole fix. The repo-side default that
regenerates it for a fresh repo is `hooks/settings-io.ts:495`:

```
{ agent: 'omp', models: ['manifest/auto'] },
```

so a complete fix touches that line too. Note the routing rule R7
(`child-skills/mk-builder/reference/lane-rules.md:72-77`): the orchestrator owns routing;
a lane must not switch its own model — which is consistent with the lead sessions above,
where the switch was a user/orchestrator action, and with this fix living in config.

Two follow-ups worth filing, not required for the fix:

- `child-skills/mk-builder/reference/lane-rules.md:79-83` (R8) could name the symptom:
  a helper whose last turn is a prose-only stop wrote nothing — expect the empty diff.
- `skills/matrixkoder/templates/brief.md:35` carries the todo-97 workaround ("an omp
  brief always writes 'none' here until that is fixed"); revert that clause once the
  model pin lands.

**Not proposed:** changing R7 to let lanes route their own helpers. That is a routing
decision and belongs to the orchestrator.

## Sources

- `~/.omp/agent/sessions/-Development-matrixkoder-mk-4/2026-10-14T12-30-55-807Z_01a13a64-ef3f-76c7-962c-4ade46b0359d/O21.jsonl` — `:3` model_change, `:4` session_init, `:36` final prose-only stop, `:37` session_exit
- same dir `O22.jsonl` — `:4`, `:41`, `:42`
- same dir `.jsonl` (the O2 lead lane) — `:3`/`:200`/`:201` model_change, `:150`/`:157` task-result, `:152`-`:156` empty diff, `:165` lead summary
- `~/.omp/agent/sessions/-Development-matrixkoder-mk-3/2026-10-14T11-18-35-664Z_01a13a22-b590-7170-9f0c-7053b3683447/{OldCodeCheck,CatalogProbe,ConfigKeys}.jsonl:4`, and `.md` reports
- `~/.omp/agent/sessions/-Development-matrixkoder-mk-3/2026-10-14T14-54-55-010Z_01a13ae8-c221-70b7-8969-648ee9f80c3a/{L0State,L0Bus,L0ToolsWake,ProbeHarness,Scout*}.jsonl:3-4`, first edits `:78` / `:134`
- `~/.omp/agent/sessions/-Development-matrixkoder-mk-3/2026-10-14T13-13-51-777Z_01a13a8c-3da1-73c6-9436-1dc598501a89.jsonl:95,101` — model switch then first write
- `~/.omp/agent/config.yml:1-4` (`model: manifest/auto`, `modelRoles`), `~/.omp/agent/models.yml:7` (`id: auto`)
- `child-skills/mk-builder/reference/lane-rules.md:72-77` (R7 routing), `:79-83` (R8), `:85-101` (R9)
- `skills/matrixkoder/templates/brief.md:34-35` (Parallel parts, the todo-97 clause)
- `hooks/settings-io.ts:495` (`omp` default pick)
- `/Users/rohits/Development/matrixkoder/.matrixkoder/matrixkoder.yaml:35,75` (omp picks, read-only)