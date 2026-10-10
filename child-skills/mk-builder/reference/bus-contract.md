# bus-contract.md — the mk-bus loopback message bus, protocol `v: 1` (`mk-bus/v1`)

Frozen by task BA.1 for MatrixKoder (plan MatrixKoder Bus Architecture, contracts C1-C4; decisions
D1-D4 binding). Sibling of `status-contract.md` (MB1.1), `lane-rules.md`, and `git-guard.md`. The
server implementation is `scripts/mk-bus.py` (BA.2). The clients are the orchestrator
(`scripts/mk-state.py`, `scripts/lane-watch.py`) and lane agents via their local tooling
(`child-skills/mk-builder/scripts/mk-builder.py`). This document defines the wire protocol, HTTP
endpoints, message schema, daemon host lifecycle, CLI interface, and acknowledge semantics for the
loopback message bus connecting the orchestrator with isolated lane sessions.

## 1. Header & Context

- **B1.** Protocol version is integer `1` (`mk-bus/v1`).
- **B2.** Background and topology: MatrixKoder coordinates multiple concurrent lane sessions (builder
  and reviewer agents running in isolated worktrees). The bus provides an asynchronous, out-of-band,
  reliable duplex communication layer over local loopback HTTP and Server-Sent Events (SSE). It
  replaces unreliable terminal pane scraping and polling tokens for structured interactions such as
  asking questions, delivering answers, sending control notices, and tracking message delivery.
- **B3.** Scope: The bus runs entirely on the local developer machine. No external network traffic,
  no cloud relay, and no remote ports are opened.

## 2. Transport and Endpoints (Contract C1)

- **B4.** Transport implementation: The bus daemon uses Python standard library
  `http.server.ThreadingHTTPServer` bound strictly to IPv4 loopback `127.0.0.1`. It NEVER binds to
  `0.0.0.0`, `localhost` (which might resolve to IPv6 `::1`), or any external interface.
- **B5.** Ephemeral port: The daemon binds to an ephemeral OS-assigned port (`port=0` at socket bind).
  The chosen port is published in `<data>/bus.json` (B21). This eliminates port conflicts between
  concurrent runs, test suites, or leftover zombie processes.
- **B6.** Loopback Host header enforcement: Every incoming HTTP request MUST include a `Host` header
  whose host portion strictly equals `127.0.0.1` or `localhost` (e.g. `127.0.0.1:<port>`). Any request
  with a missing, malformed, or external `Host` header (such as a DNS rebinding attack domain) is
  rejected immediately with HTTP 400 Bad Request:
  ```json
  {"ok": false, "error": "invalid host header"}
  ```
- **B7.** Reject browser Origin headers: The bus is exclusively for local CLI and process
  communication, not browser frontends. Any HTTP request containing an `Origin` header (or CORS
  preflight `OPTIONS` method) is rejected immediately with HTTP 403 Forbidden:
  ```json
  {"ok": false, "error": "browser origins rejected"}
  ```
  The server NEVER returns `Access-Control-Allow-Origin` or any CORS header under any circumstance.
- **B8.** Authentication: Every request—with the sole exception of `GET /health` (B9)—requires an
  HTTP `Authorization: Bearer <token>` header.
  - The token is checked against the daemon's active token store (B24).
  - An absent, malformed, or unrecognized token results in an immediate HTTP 401 Unauthorized response:
    ```json
    {"ok": false, "error": "unauthorized"}
    ```
  - **Log hygiene:** On HTTP 401, nothing is written to the message log or access log. This prevents
    token spraying, log poisoning, or denial of disk space through unauthenticated request floods.

### HTTP Endpoints

- **B9.** `GET /health`
  - Unauthenticated health probe.
  - Returns HTTP 200 OK:
    ```json
    {"ok": true, "status": "healthy", "pid": 12345, "uptime_sec": 42.5}
    ```
  - Used by `ensure` (B22) and clients to verify daemon responsiveness without needing a token.

- **B10.** `GET /events?lane=<id>` (Server-Sent Events)
  - Streams real-time message records matching the given lane.
  - Headers sent by server:
    - `Content-Type: text/event-stream; charset=utf-8`
    - `Cache-Control: no-cache`
    - `Connection: keep-alive`
    - `X-Accel-Buffering: no`
  - Event formatting:
    ```
    id: <seq>
    event: message
    data: {"seq": <seq>, "id": "m-<seq>", ...}

    ```
  - Monotonic `id:` field: The SSE `id:` field MUST strictly equal the message `seq` (integer).
  - `Last-Event-ID` replay: If the client reconnects with a `Last-Event-ID: <n>` header, the server
    first replays all recorded messages for that lane with `seq > n` from `bus.jsonl` in order, and
    then transitions seamlessly to live streaming.
  - Keepalive comments: The server emits an SSE comment line (`: keepalive\n\n`) at least once every
    15 seconds if no message is dispatched, preventing socket timeouts and detecting disconnected
    clients.

- **B11.** `GET /wait?lane=<id>&after=<seq>&timeout=<sec>` (Long-Poll Fallback)
  - Fallback endpoint for lightweight clients, one-shot scripts, or environments that cannot maintain
    a long-lived SSE stream.
  - Parameters:
    - `lane`: lane task identifier (required).
    - `after`: integer sequence number (optional, default `0`). Only messages with `seq > after` match.
    - `timeout`: integer seconds (optional, default `30`). Clamped strictly to `1 <= timeout <= 90`.
      Any requested timeout greater than 90 seconds is clamped to 90 seconds.
  - If one or more matching messages already exist with `seq > after`, the call returns immediately:
    ```json
    {"ok": true, "messages": [{"seq": 3, "id": "m-3", ...}]}
    ```
  - Otherwise, the request blocks until a matching message arrives or the timeout expires.
  - If timeout expires with no new messages, returns HTTP 200 OK with an empty array:
    ```json
    {"ok": true, "messages": []}
    ```

- **B12.** `POST /send`
  - Appends a new message to the bus.
  - Request Content-Type: `application/json`.
  - Request body: JSON object specifying `run`, `from`, `to`, `kind`, `text`, and optional `task`
    and `reply_to`.
  - Server assigns the next monotonic `seq`, formats `id: "m-<seq>"`, sets `ts` to current UTC ISO 8601,
    sets `ack_level: 1`, writes under flock to `<data>/runs/<run>/bus.jsonl`, and broadcasts to active
    SSE listeners and long-poll waiters.
  - Returns HTTP 200 OK (or 201 Created) with the complete recorded message object:
    ```json
    {"ok": true, "message": {"seq": 1, "id": "m-1", ...}}
    ```

- **B13.** `POST /ack`
  - Acknowledges message receipt or processing.
  - Request Content-Type: `application/json`.
  - Request body:
    ```json
    {"run": "<run>", "id": "<msg-id>", "ack_level": 2 | 3}
    ```
  - Valid `ack_level` values in request body are integer `2` (`delivered`) or `3` (`read`).
  - Server records the acknowledgment in `<data>/runs/<run>/bus.jsonl` under flock.
  - Returns HTTP 200 OK:
    ```json
    {"ok": true, "id": "<msg-id>", "ack_level": 3}
    ```

- **B14.** `GET /pending?run=<run>`
  - Queries all unresolved questions (`kind="ask"`) for a run.
  - A message is pending if and only if `kind == "ask"` and no message with `kind == "answer"` and
    `reply_to == ask.id` has been written to `bus.jsonl`.
  - Returns HTTP 200 OK:
    ```json
    {"ok": true, "run": "<run>", "pending": [{"seq": 1, "id": "m-1", ...}]}
    ```

## 3. Message Schema and Truth Model (Contract C2)

- **B15.** File location: `<data>/runs/<run>/bus.jsonl`.
- **B16.** Append-only under flock: The message file is strictly append-only. Every write to
  `bus.jsonl` acquires an exclusive lock (`fcntl.flock(fd, fcntl.LOCK_EX)`) on `<data>/runs/<run>/bus.lock`
  prior to seeking to EOF, writing the JSON line followed by `\n`, flushing, calling `os.fsync`, and
  releasing the lock.
- **B17.** Single source of truth: `<data>/runs/<run>/bus.jsonl` is the sole and authoritative message
  truth. The server maintains in-memory indices for speed (highest sequence number, pending asks,
  waiter queues), but upon daemon start or restart, all in-memory state is reconstructed by replaying
  `bus.jsonl` from line 1. No separate SQLite database or persistent state file exists for messages.

### Message Fields (v1)

Table 1 — Every field typed, null rules, and meaning.

| field | type | null rule | meaning |
|---|---|---|---|
| `seq` | integer >= 1 | never null | monotonic write counter per run; incremented by 1 on every append |
| `id` | string | never null | unique message identifier; format `"m-<seq>"` (e.g. `"m-1"`, `"m-42"`) |
| `ts` | string | never null | ISO 8601 UTC timestamp with trailing `Z` (e.g. `"2026-10-10T23:05:00Z"`) |
| `run` | string | never null | MK run identifier; matches `mk-state` ID alphabet (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) |
| `task` | string \| null | null allowed | task identifier (e.g. `"S1"`); `null` for run-wide or orchestrator-level notices |
| `from` | string | never null | sender identity: `"orch"` or task id `"<task>"` |
| `to` | string | never null | recipient identity: `"orch"` or task id `"<task>"` |
| `kind` | string | never null | message kind; strictly one of `"ask"`, `"answer"`, `"info"`, `"ack"` |
| `reply_to` | string \| null | null allowed | referenced message id (`"m-<n>"`), required for `"answer"` and `"ack"`, `null` otherwise |
| `text` | string | never null | message body text; non-empty for `"ask"`, `"answer"`, `"info"`; empty string `""` allowed for `"ack"` |
| `ack_level` | integer \| null | null allowed | delivery and read progress; integer `1`, `2`, `3`, or `null` |

- **B18.** Field constraints and validation:
  - `kind` MUST be one of the four legal values: `"ask"`, `"answer"`, `"info"`, `"ack"`.
  - For `kind="ask"`, `text` MUST be non-empty (whitespace-only counts as empty) and `reply_to` MUST be `null`.
  - For `kind="answer"`, `text` MUST be non-empty and `reply_to` MUST reference an existing message `id` whose `kind` was `"ask"`.
  - For `kind="ack"`, `reply_to` MUST reference the message `id` being acknowledged, and `ack_level` MUST be `2` or `3`.
  - For `kind="info"`, `text` MUST be non-empty.

### Honest Acknowledge Levels

- **B19.** Acknowledge levels measure concrete, verified stages of message transit. They are strictly
  defined as:
  - **Level 1 (`accepted` / `stored`):** The server has accepted the `POST /send` request, validated
    all fields, flushed the JSON line to `<data>/runs/<run>/bus.jsonl`, and synced to disk. Assigned
    automatically by the server upon writing.
  - **Level 2 (`delivered`):** The server has written and flushed the message frame over an active SSE
    connection (`GET /events`) or returned it in a completed long-poll response payload (`GET /wait`).
  - **Level 3 (`read`):** The client-side skill or agent script has parsed the message, displayed or
    injected it into the agent's context, and explicitly invoked `POST /ack` with `level=3`.
  - **Level 4 (`acted` — NOT a bus packet):** An observable subsequent effect in the repository or task
    lifecycle, such as the agent posting an answer, transitioning state to `working`, or creating a
    git commit. **Why Level 4 is not a bus ack packet:** A synthetic packet cannot guarantee that an
    agent acted truthfully; only externally observable artifacts (a git commit sha, a status file
    transition, or a corresponding answer message) constitute honest proof of action.

## 4. Host Lifecycle and Daemon Management (Contract C3)

- **B20.** Single instance per data directory: Exactly one `mk-bus` server process runs per data
  directory. Concurrency and startup races are governed by an exclusive lock file at `<data>/bus.lock`.
- **B21.** Metadata file `<data>/bus.json`:
  The running server's coordinates are recorded in `<data>/bus.json`:
  ```json
  {
    "pid": 54321,
    "port": 51234,
    "started": 1791627600.0,
    "owner_heartbeat": 1791627650.0
  }
  ```
  - Written atomically via temp-file replace (`os.replace`) upon server startup.
  - Removed cleanly upon graceful server shutdown.
- **B22.** `ensure --data <data> --run <run>`:
  - Idempotent daemon activator.
  - Acquires `<data>/bus.lock`.
  - Reads `<data>/bus.json` if present. If the file exists and points to an active PID whose
    `GET http://127.0.0.1:<port>/health` probe returns HTTP 200 within 500 ms, the server is healthy.
    `ensure` refreshes `owner_heartbeat` and returns immediately.
  - If `<data>/bus.json` is missing, stale, or the health probe fails:
    1. Spawns a detached background process: `python3 scripts/mk-bus.py serve --data <data>`.
    2. Polls `GET /health` until responsive (up to 5.0 seconds).
    3. Confirms `<data>/bus.json` is populated.
  - Idempotent: multiple callers calling `ensure` concurrently serialize on `<data>/bus.lock`; exactly
    one server is started.
- **B23.** Owner heartbeat and automatic stale reap:
  - The orchestrator periodically executes `mk-bus.py heartbeat --data <data>` while active.
  - The `heartbeat` command updates `owner_heartbeat` in `<data>/bus.json` to `time.time()`.
  - The server background thread inspects `owner_heartbeat` every 60 seconds.
  - **Stale exit rule:** If `time.time() - owner_heartbeat > 1800` (older than 30 minutes), the server
    assumes the orchestrator crashed or abandoned the session. The daemon logs a shutdown note,
    removes `<data>/bus.json`, and exits cleanly. This prevents orphaned background daemons from
    accumulating on developer machines.
- **B24.** Lane Tokens and Credential Isolation:
  - Each lane worktree receives a distinct bearer token scoped to that lane's task.
  - Command: `mk-bus.py token --lane <path> --task <T> --run <run>`
    1. Generates a cryptographically strong random token (`secrets.token_urlsafe(32)`).
    2. Registers the token in the server's in-memory token registry and `<data>/bus-tokens.json`
       (under flock).
    3. Writes `<lane>/.matrixkoder-bus.json` with strict POSIX permissions `0600` (`-rw-------`):
       ```json
       {
         "v": 1,
         "url": "http://127.0.0.1:51234",
         "token": "tok_9f8a1c3e...",
         "run": "settings-tab-r1",
         "task": "S1"
       }
       ```
    4. Conforms to status-contract R2 and R40: git-excluded via the `.matrixkoder-*` pattern in
       `<lane>/.git/info/exclude`. Never committed to git.
  - Command: `mk-bus.py revoke --lane <path>`
    1. Reads `<lane>/.matrixkoder-bus.json` to identify the token.
    2. Invalidates the token on the server and removes it from the token registry.
    3. Deletes `<lane>/.matrixkoder-bus.json`.

## 5. CLI Interface and Orchestrator Integration (Contract C4)

- **B25.** CLI Output Rule:
  - Every CLI invocation prints exactly ONE JSON line to stdout upon completion or failure.
  - Success outputs: `{"ok": true, ...}` with exit code `0`.
  - Failure outputs: `{"ok": false, "error": "<reason>"}` with exit code `2` (input/validation failure)
    or exit code `1` (system/network failure).
  - Never prints a Python traceback or stderr dump to stdout.
- **B26.** Server Down Rule:
  - If any client CLI command cannot reach the bus daemon (connection refused, timed out, or
    `<data>/bus.json` missing/stale), it prints:
    ```json
    {"ok": false, "error": "bus down"}
    ```
    and exits with non-zero exit code `2`.

### CLI Commands

- **B27.** `send`
  ```bash
  python3 scripts/mk-bus.py send --data <data> --run <run> --from <id> --to <id> --kind <kind> --text "<text>" [--task <T>] [--reply-to <id>]
  ```
  - Posts a message via HTTP `POST /send`.
  - Stdout:
    ```json
    {"ok": true, "message": {"seq": 1, "id": "m-1", "ts": "2026-10-10T23:02:15Z", "run": "r1", "task": "S1", "from": "S1", "to": "orch", "kind": "ask", "reply_to": null, "text": "Which fixture?", "ack_level": 1}}
    ```

- **B28.** `answer`
  ```bash
  python3 scripts/mk-bus.py answer --data <data> --run <run> <msg-id> "<text>"
  ```
  - Posts the answer via HTTP `POST /send` with `kind="answer"`, `reply_to="<msg-id>"`, `from="orch"`,
    and `to="<task>"`.
  - Identifies the task `<T>` from the original `ask` message.
  - **Orchestrator unblock integration:** Immediately invokes `mk-state.py` to restore the task status:
    ```bash
    python3 scripts/mk-state.py --run <run> task set <T> status=building --note "bus answer"
    ```
  - Stdout:
    ```json
    {"ok": true, "message": {"seq": 3, "id": "m-3", "reply_to": "m-1", ...}, "state_updated": true}
    ```

- **B29.** `pending`
  ```bash
  python3 scripts/mk-bus.py pending --data <data> --run <run>
  ```
  - Queries `GET /pending?run=<run>`.
  - Stdout:
    ```json
    {"ok": true, "run": "r1", "pending": [{"seq": 1, "id": "m-1", "kind": "ask", ...}]}
    ```

- **B30.** `tail`
  ```bash
  python3 scripts/mk-bus.py tail --data <data> --run <run> [--after <seq>] [--lane <lane>] [--follow]
  ```
  - In snapshot mode (without `--follow`): prints all matching messages as a JSON list:
    ```json
    {"ok": true, "messages": [...]}
    ```
  - In `--follow` mode: connects to `GET /events` (honouring `--after` via `Last-Event-ID`) and outputs
    each message as one JSON line as it arrives.

- **B31.** `--self-test`
  ```bash
  python3 scripts/mk-bus.py --self-test
  ```
  - Executes self-contained integration tests verifying:
    1. Ephemeral server bind and `GET /health` unauthenticated probe.
    2. Host header enforcement and rejection of external hostnames.
    3. Browser Origin header rejection (HTTP 403, no CORS headers).
    4. Bearer token authentication and HTTP 401 rejection on invalid token (no log output).
    5. SSE stream delivery (`GET /events`) with monotonic `id: <seq>`.
    6. `Last-Event-ID` message replay over SSE.
    7. Long-poll fallback (`GET /wait`) with timeout clamping (<= 90 s).
    8. Send and Ack workflows with honest Ack levels 1, 2, and 3.
    9. Pending questions index and answer resolution.
    10. `answer` triggering `mk-state.py task set <T> status=building`.
    11. Stale owner heartbeat timeout detection.
    12. Graceful server teardown.
  - Stdout on success:
    ```json
    {"ok": true, "tests": 12, "passed": 12}
    ```

## 6. Worked JSON Examples

Full JSON lines as written to `<data>/runs/<run>/bus.jsonl`.

### 1. `ask` — Builder in lane asks a clarifying question (seq 1, ack_level 1)
```json
{"seq": 1, "id": "m-1", "ts": "2026-10-10T23:02:15Z", "run": "settings-tab-r1", "task": "S1", "from": "S1", "to": "orch", "kind": "ask", "reply_to": null, "text": "Spec names two fixture formats (v2 and v3) for the config parser - which one is the acceptance target?", "ack_level": 1}
```

### 2. `ack` — Orchestrator receives and acknowledges question delivery (seq 2, ack_level 2)
```json
{"seq": 2, "id": "m-2", "ts": "2026-10-10T23:02:16Z", "run": "settings-tab-r1", "task": "S1", "from": "orch", "to": "S1", "kind": "ack", "reply_to": "m-1", "text": "", "ack_level": 2}
```

### 3. `answer` — Orchestrator posts answer resolving `m-1` (seq 3, ack_level 1)
```json
{"seq": 3, "id": "m-3", "ts": "2026-10-10T23:04:10Z", "run": "settings-tab-r1", "task": "S1", "from": "orch", "to": "S1", "kind": "answer", "reply_to": "m-1", "text": "Target format v3. Format v2 is legacy and should be deprecated per plan section 4.", "ack_level": 1}
```

### 4. `info` — Lane agent reports step completion note (seq 4, ack_level 1)
```json
{"seq": 4, "id": "m-4", "ts": "2026-10-10T23:05:00Z", "run": "settings-tab-r1", "task": "S1", "from": "S1", "to": "orch", "kind": "info", "reply_to": null, "text": "Step S1.2 unit tests passing (42 passed, 0 failed). Moving to packaging step.", "ack_level": 1}
```

## 7. Not in v1 (Deferred to Future Waves)

The following capabilities are explicitly omitted from protocol `v: 1` (`mk-bus/v1`):

- **No Forwarder into Panes:** Forwarder into agent terminal panes (the background worker running
  dispatch `--role review` or synthetic keystrokes into tmux/herdr panes) is deferred to a later wave.
  In v1, lane agents interact with the bus explicitly via their local CLI and token.
- **No Lane-to-Lane Direct Messaging:** Every message is routed through the orchestrator (`to="orch"`
  or `from="orch"`). Direct peer-to-peer lane communication (`from="S1" to="S2"`) is prohibited in v1;
  all coordination and routing remain centralized in the orchestrator.
- **No MCP Transport or Tool Definitions:** The bus does not implement Model Context Protocol (MCP)
  servers, stdio JSON-RPC bridges, or MCP tool calling schemas in v1. The transport is standard
  HTTP/1.1 loopback SSE and REST JSON.
- **No Claude Code Push Channels, Plugin Monitors, or OpenCode `--port` Push:** The bus avoids
  invasive internal hooks, undocumented Claude Code extension channels, or experimental OpenCode
  `--port` push tunnels. Interaction remains strictly out-of-band via loopback HTTP and local CLI.

## 8. Open Points Decided Here

- **Ephemeral port over static port:** Eliminates port collision risks across parallel test runs,
  multi-repo development, or lingering zombie processes.
- **`bus.jsonl` append-only truth:** Eliminates embedded database dependencies (e.g. SQLite locks).
  A single file append under POSIX `flock` guarantees durability, crash recovery, and instant auditability.
- **Loopback Host and Origin rejection:** Prevents browser-based Cross-Origin attacks and DNS
  rebinding, ensuring local web browsers cannot interact with the bus daemon.
- **`answer` triggers `mk-state.py`:** Automatically moving a task back to `building` upon receiving
  a bus answer prevents orphaned `blocked` states and eliminates manual user intervention.
- **Stale owner heartbeat auto-reap (30 minutes):** Daemons automatically shut down if the parent
  orchestrator session disappears, preventing headless process leaks.
- **Level 4 (`acted`) not a bus packet:** A bus protocol cannot verify intent or cognitive receipt;
  honest proof of action is observed exclusively through downstream repository state (git commits,
  reports, and state transitions).
