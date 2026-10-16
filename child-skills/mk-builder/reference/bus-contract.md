# bus-contract.md — the mk-bus loopback message bus, protocol `v: 1` (`mk-bus/v1`)

Frozen by task BA.1 for MatrixKoder (plan MatrixKoder Bus Architecture, contracts C1-C4; decisions
D1-D4 binding; updated in BA FIX R2 for review findings and security hardening; updated in BA FIX
R4/R5 for the re-review findings: lane-scoped acks on every path, strict B18/B13 typing, OPTIONS
JSON, ensure probe semantics, and honest B31 coverage). Sibling of
`status-contract.md` (MB1.1), `lane-rules.md`, and `git-guard.md`. The server implementation is
`scripts/mk-bus.py` (BA.2). The clients are the orchestrator (`scripts/mk-state.py`,
`scripts/lane-watch.py`) and lane agents via their local tooling
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
  The chosen port is published in `<home>/bus/bus.json` (B21). This eliminates port conflicts between
  concurrent runs, test suites, or leftover zombie processes.
  A respawn (`ensure`/`heartbeat`) first re-tries the dead bus's previous port, so armed lanes' url
  stays valid, and falls back to a fresh ephemeral port when it is taken.
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
- **B8.** Authentication and Token Scoping: Every request—with the sole exception of `GET /health` (B9)—requires
  an HTTP `Authorization: Bearer <token>` header.
  - **Constant-time verification:** The server verifies incoming tokens against the active token store
    using `hmac.compare_digest` to prevent timing attacks.
  - **Token roles:**
    - **Orchestrator token (`role="orch"`):** Has full privileges. May send answers, send to any lane,
      read `/pending`, and override sender/recipient identities.
    - **Lane token:** Bound strictly to `{run, task}`. For lane tokens, the server pins `run = tok.run`,
      `task = tok.task`, `from = tok.task`, forces `to = "orch"`, and restricts `kind` to `ask | info | ack`.
      Any violation results in HTTP 403 Forbidden:
      ```json
      {"ok": false, "error": "lane tokens cannot send answers"}
      ```
      Lane tokens are strictly forbidden from reading `/pending` (HTTP 403). Acknowledgment requests from
      a lane token are restricted to messages addressed to that lane.
  - **Path traversal validation:** Every endpoint strictly validates `run` and `task` identifiers against
    regex `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (no `..`, no `/`). Any failure results in HTTP 400 Bad Request
    (`{"ok": false, "error": "invalid run id"}` or `{"ok": false, "error": "invalid task id"}`), and nothing
    is written to disk.
  - **Unauthorized response:** An absent, malformed, or unrecognized token results in an immediate HTTP 401
    Unauthorized response:
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

- **B10.** `GET /events?lane=<id>&run=<run>` (Server-Sent Events)
  - Streams real-time message records matching the given lane.
  - Path traversal: `run` and `lane` are strictly validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
    (HTTP 400 on failure).
  - Token scoping: For lane tokens, `run` and `lane` are pinned to `tok.run` and `tok.task`. Any attempt
    to stream another lane or run returns HTTP 403.
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
    first replays all recorded messages for that lane with `seq > n` from the project DB (or the run's
    archived `bus.jsonl` once archived) in order, and then transitions seamlessly to live streaming.
  - Keepalive comments: The server emits an SSE comment line (`: keepalive\n\n`) at least once every
    15 seconds if no message is dispatched, preventing socket timeouts and detecting disconnected
    clients.

- **B11.** `GET /wait?lane=<id>&after=<seq>&timeout=<sec>&run=<run>` (Long-Poll Fallback)
  - Fallback endpoint for lightweight clients, one-shot scripts, or environments that cannot maintain
    a long-lived SSE stream.
  - Path traversal: `run` and `lane` are strictly validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
    (HTTP 400 on failure).
  - Token scoping: For lane tokens, `run` and `lane` are pinned to `tok.run` and `tok.task`.
  - Parameters:
    - `run`: run identifier (required).
    - `lane`: lane task identifier (required).
    - `after`: integer sequence number (optional, default `0`). Only messages with `seq > after` match.
    - `timeout`: finite number of seconds (optional, default `30`). Clamped strictly to `1 <= timeout <= 90`.
      Non-finite or NaN values default to `30`.
  - Response format:
    If one or more matching messages already exist with `seq > after`, the call returns immediately:
    ```json
    {"ok": true, "message": {"seq": 3, "id": "m-3", ...}, "messages": [{"seq": 3, "id": "m-3", ...}]}
    ```
    Otherwise, the request blocks until a matching message arrives or the timeout expires.
    If timeout expires with no new messages, returns HTTP 200 OK:
    ```json
    {"ok": true, "message": null, "messages": []}
    ```
    Both `message` (single latest/first matching message or `null`) and `messages` (list of matching messages
    or `[]`) are returned for dual-client contract parity.

- **B12.** `POST /send`
  - Appends a new message to the bus.
  - Request Content-Type: `application/json`.
  - Input validation:
    - Request body MUST be a JSON object (HTTP 400 `{"ok": false, "error": "request body must be a JSON object"}`
      otherwise).
    - Request size limit: Maximum length 64 KiB (65536 bytes); requests exceeding this limit return HTTP 413.
    - Path traversal: `run` and `task` are strictly validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
      (HTTP 400 on failure).
    - Token scoping:
      - Orchestrator token (`role="orch"`): May specify any `run`, `task`, `from`, `to`, and `kind`.
      - Lane token: Bound to `{run, task}`. Server pins `run = tok.run`, `task = tok.task`, `from = tok.task`,
        forces `to = "orch"`, and restricts `kind` to `ask | info | ack`. Mismatched values in request body are
        rejected with HTTP 403. Any attempt by a lane token to send `kind="answer"` returns HTTP 403
        `{"ok": false, "error": "lane tokens cannot send answers"}`.
    - Message fields validation (B18):
      - `kind` MUST be one of `{"ask", "answer", "info", "ack"}`.
      - `text` MUST be a string with byte length <= 64 KiB. Non-empty for `ask`, `answer`, `info`. Empty string `""`
        allowed for `ack`.
      - `reply_to`: Required for `answer` (must name an existing unanswered ask in that run) and `ack` (must
        name an existing message in that run). Null for `ask` and `info`.
  - Server assigns monotonic `seq`, formats `id: "m-<seq>"`, sets `ts` to current UTC ISO 8601 with trailing
    `Z` (`%Y-%m-%dT%H:%M:%SZ`), sets `ack_level: 1`, writes the `chan='bus'` row in the project DB, and
    broadcasts to active SSE listeners and long-poll waiters.
  - Returns HTTP 200 OK with the complete recorded message object:
    ```json
    {"ok": true, "message": {"seq": 1, "id": "m-1", ...}}
    ```

- **B13.** `POST /ack`
  - Acknowledges message receipt or processing.
  - Request Content-Type: `application/json`.
  - Input validation:
    - Request body MUST be a JSON object (HTTP 400 otherwise).
    - Path traversal: `run` is strictly validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (HTTP 400 on failure).
    - Token scoping: For lane tokens, `run` is pinned to `tok.run`. Lane tokens can only acknowledge messages
      addressed to their lane (`to == tok.task` or `task == tok.task`); attempts to ack messages for other
      lanes return HTTP 403 `{"ok": false, "error": "cannot ack messages for other lanes"}`.
    - Request body fields:
      ```json
      {"run": "<run>", "id": "<msg-id>", "ack_level": 2 | 3}
      ```
    - `id`: Must reference an existing message recorded in the project DB.
    - `ack_level`: Integer in `1..3` (strictly typed: a JSON `true`/`false` is rejected with HTTP 400).
      Levels `2` or `3` are the normal use.
  - Server records the acknowledgment in the project DB (`acks`, plus an `events` row with
    `kind='ack'` so replay stays ordered). Ack records are
    stored as `kind="ack"` rows and are never added to the message index (live or replayed) — only
    the target message's `ack_level` changes.
  - Returns HTTP 200 OK:
    ```json
    {"ok": true, "id": "<msg-id>", "ack_level": 3}
    ```

- **B14.** `GET /pending?run=<run>`
  - Queries all unresolved questions (`kind="ask"`) for a run.
  - Path traversal: `run` is strictly validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (HTTP 400 on failure).
  - Token scoping: Restricted strictly to the orchestrator token (`role="orch"`). Lane tokens are forbidden
    from reading `/pending` and receive HTTP 403:
    ```json
    {"ok": false, "error": "forbidden: lane tokens cannot read pending asks"}
    ```
  - A message is pending if and only if `kind == "ask"` and no message with `kind == "answer"` and
    `reply_to == ask.id` has been recorded in the project DB.
  - Returns HTTP 200 OK:
    ```json
    {"ok": true, "run": "<run>", "pending": [{"seq": 1, "id": "m-1", ...}]}
    ```

## 3. Message Schema and Truth Model (Contract C2)

- **B15.** Live location: the project DB `<home>/projects/<slug>/mk.db` — an `events` row with
  `chan='bus'` per message (C6, §9). At run end the run's messages and acks are archived,
  count-checked, to `<home>/projects/<slug>/plans/<plan>/runs/<run>/bus.jsonl` and leave the DB.
  (`<data>/runs/<run>/bus.jsonl` as the live file was the pre-`projects/` layout, until 2026-10-15.)
- **B16.** Append-only under flock: message rows are appended, never rewritten. State files and lock files
  are strictly created and maintained with mode `0600` (`-rw-------`, enforced via `os.fchmod` on open/create).
  Every append opens the project DB, takes a write transaction, writes the row, and commits;
  the run-end archive writes `bus.jsonl` under an exclusive `fcntl.flock(fd, fcntl.LOCK_EX)`
  (seeking to EOF, writing each JSON line followed by `\n`, flushing, `os.fsync`, releasing).
- **B17.** Single source of truth: the project DB's `events`/`acks` rows are the sole and authoritative message
  truth while the run is live. The server maintains in-memory indices for speed (highest sequence number, pending asks,
  waiter queues), but upon daemon start or restart, all in-memory state is reconstructed by replaying
  the run's rows from the first. Non-dict or malformed rows are skipped cleanly during replay. The
  archived `bus.jsonl` is a read-only copy written once at run end. No separate SQLite message DB exists.

### Message Fields (v1)

Table 1 — Every field typed, null rules, and meaning.

| field | type | null rule | meaning |
|---|---|---|---|
| `seq` | integer >= 1 | never null | monotonic write counter per run; incremented by 1 on every append |
| `id` | string | never null | unique message identifier; format `"m-<seq>"` (e.g. `"m-1"`, `"m-42"`) |
| `ts` | string | never null | ISO 8601 UTC timestamp with trailing `Z` (e.g. `"2026-10-10T23:05:00Z"`) |
| `run` | string | never null | MK run identifier; strictly matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` |
| `task` | string \| null | null allowed | task identifier (e.g. `"S1"`); strictly matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` or `null` for run-wide notices |
| `from` | string | never null | sender identity: `"orch"` or task id `"<task>"` |
| `to` | string | never null | recipient identity: `"orch"` or task id `"<task>"` |
| `kind` | string | never null | message kind; strictly one of `"ask"`, `"answer"`, `"info"`, `"ack"` |
| `reply_to` | string \| null | null allowed | referenced message id (`"m-<n>"`), required for `"answer"` and `"ack"`, `null` otherwise |
| `text` | string | never null | message body text (byte length <= 64 KiB); non-empty for `"ask"`, `"answer"`, `"info"`; empty string `""` allowed for `"ack"` |
| `ack_level` | integer \| null | null allowed | delivery and read progress; integer `1`, `2`, `3`, or `null` |

- **B18.** Field constraints and input validation:
  - Request body MUST be a JSON object (HTTP 400 `{"ok": false, "error": "request body must be a JSON object"}`
    otherwise).
  - Path traversal: `run` and `task` MUST strictly match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (no `..`, no `/`).
    Any invalid identifier returns HTTP 400 `{"ok": false, "error": "invalid run id"}` or
    `{"ok": false, "error": "invalid task id"}`, and nothing is written to disk.
  - `kind` MUST be one of the four legal values: `"ask"`, `"answer"`, `"info"`, `"ack"`.
  - `text` MUST be a string with byte length <= 64 KiB (65536 bytes). Non-empty for `ask`, `answer`,
    `info`; empty string `""` allowed for `ack`.
  - For `kind="ask"` and `kind="info"`, `reply_to` MUST be `null`.
  - For `kind="answer"`, `reply_to` MUST reference an existing message `id`
    in that run whose `kind` was `"ask"` and which has not yet received an answer.
  - For `kind="ack"` (via `POST /send`), `reply_to` MUST be present and MUST reference an existing
    message `id` in that run being acknowledged.
  - For `GET /wait`, `timeout` MUST be a finite number; non-finite or NaN values default to 30. Clamped
    strictly to `1 <= timeout <= 90`.
  - All input validations occur before state files are touched. Malformed or invalid input immediately
    returns HTTP 400 with a descriptive JSON error payload; the server never raises unhandled exceptions
    to the HTTP client.

### Honest Acknowledge Levels

- **B19.** Acknowledge levels measure concrete, verified stages of message transit. They are strictly
  defined as:
  - **Level 1 (`accepted` / `stored`):** The server has accepted the `POST /send` request, validated
    all fields, written the event row (`chan='bus'`) to the project DB, and committed. Assigned
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
  - **Persistence rule:** Level 2 (`delivered`) is tracked in server memory only — it is NOT written
    to `bus.jsonl` and resets to 1 on server restart. Only explicit acknowledgments (`POST /ack`, or
    `POST /send` with `kind="ack"`) are persisted as `kind="ack"` records; on replay they raise the
    target message's `ack_level`, and ack records themselves are never replayed as messages.

## 4. Host Lifecycle and Daemon Management (Contract C3)

- **B20.** Single instance per home: Exactly one `mk-bus` server process runs per MK home. Concurrency
  and startup races are governed by an exclusive lock file at `<home>/bus/bus.lock` created with mode `0600`.
- **B21.** Metadata file `<home>/bus/bus.json`:
  The running server's coordinates and orchestrator bearer token are recorded in `<home>/bus/bus.json`:
  ```json
  {
    "pid": 54321,
    "port": 51234,
    "started": 1791627600.0,
    "owner_heartbeat": 1791627650.0,
    "token": "tok_orch_9f8a1c3e..."
  }
  ```
  - Mode `0600` (`-rw-------`) strictly enforced via `os.fchmod` on file descriptor creation and atomic write.
  - Written atomically via temp-file replace (`os.replace`) upon server startup.
  - Removed cleanly upon graceful server shutdown.
- **B22.** `ensure --data <data> --run <run>`:
  - Idempotent daemon activator.
  - Acquires `<home>/bus/bus-ensure.lock` (mode `0600`), a dedicated lock that serializes concurrent
    `ensure` calls. The running server separately holds `<home>/bus/bus.lock` (B20); `ensure` itself
    never takes `bus.lock`.
  - Reads `<home>/bus/bus.json` if present. If the file exists and points to a live PID, `ensure`
    probes `GET http://127.0.0.1:<port>/health` with a 1.0 s timeout, retrying for up to 5.0 s
    while the PID stays alive. A 200 response means healthy: `ensure` refreshes `owner_heartbeat`
    and returns immediately with `started: false`.
  - `<home>/bus/bus.json` is removed ONLY when the recorded PID is dead (or the file is missing or
    unreadable). A live server whose health probe fails transiently is never declared down: no
    respawn is attempted (a second `serve` could not take `bus.lock` anyway), and `ensure` fails
    with `{"ok": false, "error": "bus process alive but not answering health probes"}` instead of
    destroying live state.
  - When no healthy server exists:
    1. Spawns a detached background process: `python3 scripts/mk-bus.py serve --data <data>`.
    2. Polls `GET /health` until responsive (up to 10.0 seconds).
    3. Confirms `<home>/bus/bus.json` is populated.
  - Idempotent: multiple callers calling `ensure` concurrently serialize on `<home>/bus/bus-ensure.lock`;
    exactly one server is started, and `started: true` is reported only for the winning PID.
- **B23.** Owner heartbeat and automatic stale reap:
  - The orchestrator periodically executes `mk-bus.py heartbeat --data <data>` while active.
  - The `heartbeat` command updates `owner_heartbeat` in `<home>/bus/bus.json` to `time.time()`.
  - The server background thread inspects `owner_heartbeat` every 60 seconds (or test interval).
  - **Stale exit rule:** If `<home>/bus/bus.json` is missing/removed, or if `time.time() - owner_heartbeat > 1800`
    (older than 30 minutes), the server assumes the orchestrator crashed, deleted state, or abandoned the
    session. The daemon logs a shutdown note, removes `<home>/bus/bus.json`, and exits cleanly. This prevents
    orphaned background daemons from accumulating on developer machines.
- **B24.** Token Scoping, Credential Isolation, and File Permissions:
  - **Strict file permissions (0600):** State and credential files—specifically `<home>/bus/bus.json`,
    `<home>/bus/bus-tokens.json`, `<home>/bus/bus.lock`, `<home>/bus/bus-ensure.lock`,
    `<home>/bus/tokens.lock`, `<home>/bus/lanes/<sha256(lane)>.json`, and the run-end
    `<home>/projects/<slug>/plans/<plan>/runs/<run>/bus.jsonl`—MUST have strict
    POSIX permissions `0600` (`-rw-------`). The directory `<home>/bus/` and the run archive's
    `plans/<plan>/runs/<run>/` are mode `0700`. Permissions are enforced via `os.fchmod` on descriptor
    creation and update.
  - **Constant-time token verification:** Incoming bearer tokens are verified using `hmac.compare_digest`
    against registered tokens.
  - **Token roles:**
    - **Orchestrator token (`role="orch"`):** Generated at server start, stored in `<home>/bus/bus.json` (0600)
      and `<home>/bus/bus-tokens.json` (0600). Allowed to send answers, send to any lane, read `/pending`,
      and override sender/recipient.
    - **Lane tokens:** Scoped strictly to `{project, run, task}`. Generated via `mk-bus.py token --lane <path> --task <T> --run <run>`.
      The reserved task id `"orch"` is refused (`invalid`/reserved error, exit 2) — a lane token can
      never carry the orchestrator identity. Registered in `<home>/bus/bus-tokens.json` and written to
      `<home>/bus/lanes/<sha256(realpath(lane))>.json` (0600):
      ```json
      {
        "v": 1,
        "url": "http://127.0.0.1:51234",
        "token": "tok_lane_9f8a1c3e...",
        "project": "hermes-switchui",
        "run": "settings-tab-r1",
        "task": "S1"
      }
      ```
      The token's `project` slug resolves to the project DB through `mkpaths.project_db(slug)`, so the
      bus follows every switch. For lane tokens, the server pins `run = tok.run`, `task = tok.task`,
      `from = tok.task`, forces `to = "orch"`, and restricts `kind` to `ask | info | ack` (returns
      HTTP 403 on violations). Acks are restricted to messages addressed to that lane. Reading
      `/pending` is forbidden (HTTP 403).
    - **CLI token preference (known limit):** the orchestrator CLI commands (`send`, `answer`,
      `pending`) always prefer the orchestrator token in `<home>/bus/bus.json` and only fall back to a
      lane token file when `bus.json` carries none; the lane file's `url` is
      never used by the CLI. Lane token scoping therefore holds against lanes that cannot read
      `<home>/bus/bus.json`; a lane that can read it obtains the orchestrator token regardless. Lane
      agents use their own scoped token over HTTP directly, not via the CLI.
    - Conforms to status-contract R2 and R40: the token file lives outside the lane under
      `<home>/bus/lanes/`, git-excluded and never committed to git.
  - Command: `mk-bus.py revoke --lane <path>`
    1. Reads `<home>/bus/lanes/<sha256(realpath(lane))>.json` to identify the token.
    2. Invalidates the token on the server and removes it from `<home>/bus/bus-tokens.json`.
    3. Deletes the lane token file.

## 5. CLI Interface and Orchestrator Integration (Contract C4)

- **B25.** CLI Output and Error Mapping Rule:
  - Every CLI invocation prints exactly ONE JSON line to stdout upon completion or failure.
  - Success outputs: `{"ok": true, ...}` with exit code `0`.
  - Failure outputs: `{"ok": false, "error": "<reason>"}` with exit code `2`.
  - **Client error mapping:** On HTTP client errors (400 Bad Request, 401 Unauthorized, 403 Forbidden,
    413 Payload Too Large) or server errors (500), the CLI maps the response to
    `{"ok": false, "error": "HTTP <status>: <server error>"}` and exits with code `2`.
  - **Server Down Rule:** The error `"bus down"` (`{"ok": false, "error": "bus down"}`) is strictly
    reserved for connection failures (connection refused, timed out, or `<home>/bus/bus.json` missing/stale).
  - Never prints a Python traceback or stderr dump to stdout.
- **B26.** Server Down Rule:
  - If any client CLI command cannot reach the bus daemon due to a network connection failure (connection
    refused, timed out, or `<home>/bus/bus.json` missing/stale), it prints:
    ```json
    {"ok": false, "error": "bus down"}
    ```
    and exits with exit code `2`.

### CLI Commands

- **B27.** `send`
  ```bash
  python3 scripts/mk-bus.py send --data <data> --run <run> --from <id> --to <id> --kind <kind> --text "<text>" [--task <T>] [--reply-to <id>]
  ```
  - Posts a message via HTTP `POST /send`.
  - Strictly validates `--run` and `--task` against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.
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
  - Strictly requires `<msg-id>` to exist as an unanswered `ask` in that run.
  - Identifies the task `<T>` from the original `ask` message.
  - **Orchestrator unblock integration:** Immediately invokes `mk-state.py` to restore the task status,
    always passing `--data <data>`:
    ```bash
    python3 scripts/mk-state.py --data <data> --run <run> task set <T> status=building --note "bus answer"
    ```
  - Reports `state_updated` boolean and any `mk_state_error` message instead of swallowing failures.
  - Stdout:
    ```json
    {"ok": true, "message": {"seq": 3, "id": "m-3", "reply_to": "m-1", ...}, "state_updated": true, "mk_state_error": null}
    ```

- **B29.** `pending`
  ```bash
  python3 scripts/mk-bus.py pending --data <data> --run <run>
  ```
  - Queries `GET /pending?run=<run>` using the orchestrator token.
  - Stdout:
    ```json
    {"ok": true, "run": "r1", "pending": [{"seq": 1, "id": "m-1", "kind": "ask", ...}]}
    ```

- **B30.** `tail`
  ```bash
  python3 scripts/mk-bus.py tail --data <data> --run <run> [--after <seq>] [--lane <lane>] [--n <count>]
  ```
  - Snapshot mode (v1): reads the run's messages from the project DB (or its archived
    `plans/<plan>/runs/<run>/bus.jsonl` once the run has closed) and prints the matching
    messages (filtered by `--after` seq and `--lane`, last `--n` by default, default `--n 10`):
    ```json
    {"ok": true, "messages": [...]}
    ```
    If `--run` is omitted and exactly one run is live in the project DB, that run is used.
  - `--follow` is NOT implemented in v1 (deferred; see section 7).

- **B31.** `--self-test`
  ```bash
  python3 scripts/mk-bus.py --self-test
  ```
  - Executes a self-contained integration test (stdlib only, temp data dir, real server on an
    ephemeral 127.0.0.1 port, real HTTP from an in-test client, real `mk-state.py`). Coverage:
    1. `ensure` idempotence: two calls → one process (`started: false`, same pid/port), plus the
       unauthenticated `GET /health` probe returning `pid` and `uptime_sec`.
    2. Concurrent `ensure` race: 4 simultaneous callers serialize on `bus-ensure.lock`; exactly one
       reports `started: true`; all agree on pid/port.
    3. Token minting: lane token file POSIX mode `0600` carrying `"v": 1`; the reserved task id `orch` refused;
       state files (`bus.json`, `bus-tokens.json`, `bus.lock`, `bus-ensure.lock`, `tokens.lock`)
       mode `0600`.
    4. Path traversal: `run` ids containing `..`, `/`, NUL or empty → HTTP 400; nothing written
       outside the project DB.
    5. Lane token scoping: lane tokens cannot send answers, send peer-to-peer, override `run`, read
       `/pending`, or stream/long-poll another lane or run (`/events`, `/wait`) → HTTP 403.
    6. Auth: wrong, absent, and non-ASCII bearer tokens → HTTP 401 with no new project-DB rows.
    7. Input validation over real HTTP: non-dict body, bad `kind`, empty `text` for ask, `reply_to`
       on ask/info, ack without `reply_to`, unknown `reply_to`, boolean `ack_level` (both `/send`
       and `/ack`), bodies over 64 KiB → HTTP 413, external `Host` → HTTP 400, `Origin` header and
       `OPTIONS` preflight → HTTP 403 JSON.
    8. `/wait` timeout clamp over real HTTP: `timeout=0.2` clamps up to 1 s; `timeout=nan` falls
       back to a finite 30 s wait (stays open until a matching message arrives, no error).
    9. ask → answer round trip in under 2 s with honest ack levels (1 → 3 via `/ack`).
    10. Cross-lane ack forgery refused on every ack path: `POST /send` with `kind="ack"` and
        `POST /ack` → HTTP 403, nothing written.
    11. Archived `bus.jsonl` shape: every C2 field present on each line, UTC `Z` timestamps, POSIX mode
        `0600`, the run archive directory mode `0700`.
    12. SSE replay via `Last-Event-ID` with `Content-Type: text/event-stream; charset=utf-8` and
        monotonic `id:` events after the given seq.
    13. Lane ack via `POST /send` records an ack record (never a message): the record id is not
        addressable live, and `/wait` + `/pending` state is identical after a restart
        (live state == replayed state).
    14. Stale `owner_heartbeat` self-exit and missing-`bus.json` reaper.
    15. Server down → `send` prints `{"ok": false, "error": "bus down"}`, non-zero exit, no traceback.
    16. `ensure` against a live but unresponsive (SIGSTOPed) server: `bus.json` kept, no respawn,
        error surfaced; recovery after `SIGCONT`.
    17. `answer` integration with the real `mk-state.py`: task status becomes `building`,
        `state_updated`/`mk_state_error` reported.
    18. Ambiguous message id: `answer m-1` without `--run` when several runs hold `m-1` →
        `{"ok": false, "error": "ambiguous message id, pass --run"}`; `--run` disambiguates.
  - Stdout format: prints banner followed by JSON summary:
    ```
    pass mk-bus.py: mk-bus self-test ok
    {"ok": true, "tests": 18, "passed": 18}
    ```
    The `tests`/`passed` counts are COMPUTED from the checks that actually ran — never a literal.
  - Not covered by the self-test (verified by review probes instead): `: keepalive` comment cadence
    on a 15 s idle stream, token revocation on a running server, and constant-time properties of
    the token comparison (only its 401 behavior is tested).

## 6. Worked JSON Examples

Full JSON lines as archived to `<home>/projects/<slug>/plans/<plan>/runs/<run>/bus.jsonl` at run end
(live, the same records are `events` rows with `chan='bus'` in the project DB).

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
- **No `tail --follow` in v1:** `tail` is snapshot-only (reads the project DB, or the run's archived
  `bus.jsonl` once closed, with `--after`, `--lane`, `--n` filters). Live-following via `GET /events`
  SSE is deferred to a later wave.
- **No MCP Transport or Tool Definitions:** The bus does not implement Model Context Protocol (MCP)
  servers, stdio JSON-RPC bridges, or MCP tool calling schemas in v1. The transport is standard
  HTTP/1.1 loopback SSE and REST JSON.
- **No Claude Code Push Channels, Plugin Monitors, or OpenCode `--port` Push:** The bus avoids
  invasive internal hooks, undocumented Claude Code extension channels, or experimental OpenCode
  `--port` push tunnels. Interaction remains strictly out-of-band via loopback HTTP and local CLI.

## 8. Open Points Decided Here

- **Ephemeral port over static port:** Eliminates port collision risks across parallel test runs,
  multi-repo development, or lingering zombie processes.
- **Project DB as live message truth, `bus.jsonl` as the archive:** Live messages are `events` rows
  (`chan='bus'`) in the project DB; at run end they are count-checked and archived to
  `plans/<plan>/runs/<run>/bus.jsonl` under POSIX `flock`. One store, one archive, instant
  auditability — no separate message database and no per-run live file.
- **Loopback Host and Origin rejection:** Prevents browser-based Cross-Origin attacks and DNS
  rebinding, ensuring local web browsers cannot interact with the bus daemon.
- **Strict path traversal validation:** Enforcing regex `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` on `run`
  and `task` prevents directory traversal attacks and invalid filesystem writes across all HTTP endpoints
  and CLI arguments.
- **Token scoping and least privilege:** Restricting lane tokens to their bound `{project, run, task}`
  and forcing `to="orch"`, while disallowing `/pending` or sending answers, prevents privilege
  escalation and cross-lane interference.
- **Mode 0600 on state and credential files:** `<home>/bus/bus.json`, `<home>/bus/bus-tokens.json`,
  `<home>/bus/bus.lock`, and the lane token file `<home>/bus/lanes/<sha256(lane)>.json` are strictly
  mode `0600` via `os.fchmod` to prevent local snooping.
- **Constant-time token verification:** `hmac.compare_digest` prevents side-channel timing attacks on
  bearer tokens.
- **`answer` triggers `mk-state.py`:** Automatically moving a task back to `building` upon receiving
  a bus answer prevents orphaned `blocked` states and eliminates manual user intervention; failure states
  are surfaced via `state_updated` and `mk_state_error`.
- **Stale owner heartbeat auto-reap (30 minutes):** Daemons automatically shut down if the parent
  orchestrator session disappears or if `bus.json` is deleted, preventing headless process leaks.
- **Level 4 (`acted`) not a bus packet:** A bus protocol cannot verify intent or cognitive receipt;
  honest proof of action is observed exclusively through downstream repository state (git commits,
  reports, and state transitions).


## 9. The project store (C6)

Messages live in the project DB, `<home>/projects/<slug>/mk.db` — the only source of truth.
`mkpaths.project_db(slug)` resolves it, so the bus follows every switch. Bus files live under
`<home>/bus/` (C6, §10); no bus DB exists and no per-run `bus.jsonl` is written live.

- **Where messages live.** Every live bus message is an `events` row with `chan='bus'` in the
  project DB. The run, task, sender, recipient, kind, reply, and text are columns on that row.
  Acks are rows in `acks` (and an `events` row with `kind='ack'` so replay stays ordered), and a
  `report`/`review` message stores a bus `artifacts` row.
- **Run-end archive.** When a run closes, its bus messages, acks and bus artifacts are written to
  `<home>/projects/<slug>/plans/<plan>/runs/<run>/bus.jsonl` (count-checked against the rows) and
  leave the DB. Cursors tolerate the gap: the orchestrator's watch cursors (`watch_cursors` rows in
  the project DB) skip over archived seqs without replaying or
  stalling. A token's `project` slug finds the project DB through `mkpaths.project_db(slug)`.
- **Id semantics.** The event row id returned by `append_event` is the SSE `Last-Event-ID` and the
  message `seq`. The wire `id` is derived as `m-<row id>` and is not stored in `meta`. A client
  that reconnects with `Last-Event-ID: <n>` is resumed at rows with id greater than `n` (or at the
  first archived line past `n`). A restart replays from the store: the same rows, the same wire
  ids, no duplicate, none dropped.
- **Kinds.** Accepted kinds are `ask`, `answer`, `info`, `ack`, plus `task`, `status`, `commit`,
  `report`, `review`, `nudge`. Lane tokens may send `task`, `commit`, `report`, `review`, and
  `nudge`. Lane tokens may not send `status` or `answer`. `mk-builder.py report|review-done --stdin` send `report`/`review` bodies; `note --kind proof --stdin` sends an `info` whose first line is `proof round: <n>`. (Before the projects/ layout, until
  2026-10-15, only `ask`, `answer`, `info`, `ack` were accepted.)
- **Artifacts.** A `report` or `review` message becomes an artifact for that task, round, and
  kind, plus the bus event pointing at it. The round is the integer after a leading `round:N`
  line; otherwise the round is 0. Sending the same task, round, and kind again stores `rev+1`.
  The body may be paged with the `\x1e` separator and is at most 128 KiB. A larger body is refused.
- **Status.** A `status` message calls `transition()` and never writes the task row itself. The
  text is the new status. The status is validated and `transition()` runs before any bus event is
  stored or streamed. An unknown status or a rejected transition is HTTP 400: nothing in `events`,
  nothing in memory, nothing on SSE. If the transition commits and the bus event then fails, the
  reply is HTTP 500 (the state change stands; resending the status is idempotent). Views and the
  root card update because `transition()` is the only status writer.
- **Connections.** One store connection per operation. No read transaction is held across an SSE
  write, so a slow SSE client cannot lock the store.
- **Tokens.** A lane token is bound to one run and one task. It cannot write another run. Project
  isolation is the project DB: a token minted for project A's `mk.db` is not in project B's token
  file, so it cannot open B's store. `/mk setup` creates the project DB.


## 10. Root bus (C6)

One bus per home: `<home>/bus/bus.json`, `<home>/bus/bus-tokens.json`, `<home>/bus/lanes/<sha256(lane)>.json`
and `<home>/bus/ring-queue.json` (`bus/bus.lock` guards the single instance; before the projects/
layout, until 2026-10-15, §4's `<data>/...` names were the live paths). The orchestrator's bus watch
cursors live in the project DB `watch_cursors` table. The process exits only when no
project has heartbeated within the owner timeout.

- **Tokens.** A lane token is scoped to one project + run + task. An orchestrator token is scoped to one project. A root token is read-only: every POST, PUT, and DELETE returns 403 before the body is parsed. Every request resolves one project from the token; a query or body project that disagrees is 403 and changes nothing. An empty project slug is refused. Run state is cached by `(project, run)`.
- **Token file.** The lane token is written to `<home>/bus/lanes/<sha256(realpath(lane))>.json`, mode 0600, atomically (temp file in the same directory, fsync, replace). `MK_BUS_TOKEN_FILE` overrides that path.
- **Doorbell.** An unsolicited `nudge`, `info`, or answer-without-waiter addressed to an idle lane is typed once as the fixed line `Run: python3 <skill>/scripts/mk-builder.py inbox then carry out what it delivers (brief, round, nudge) without asking; ask only over the bus for a real decision.` A working lane is queued; the queue drains on a 5s interval (`MK_BUS_RING_INTERVAL_SEC`) and when the lane goes idle. At most one ring per message id. The ring event is recorded before the line is typed; a recorded-but-untyped ring is typed once on restart, a typed one never again.
- **Metrics.** `mk-bus.py metrics --run <id>` prints one JSON line: answer latency p50/p95, failed deliveries, obedience, false-STALL count.
- **Lane status.** A lane token cannot POST `status`. Lanes announce status as a `task` message whose text is `status <mapped>` plus `round: <n>`.
