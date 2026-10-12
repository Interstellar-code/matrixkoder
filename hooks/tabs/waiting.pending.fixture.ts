// Captured from a real temp bus, not hand-written. Setup (run once, outside the
// plugin test runtime, which cannot spawn): `mk-bus.py ensure --data <temp>/d
// --run r1`, then `mk-bus.py send --run r1 --task T1 --kind ask --text "how to
// proceed?" --from T1 --to orch --data <temp>/d`, then `mk-bus.py pending
// --data <temp>/d` (no --json: the command has none and prints JSON already).
// The bus was stopped with `mk-bus.py down` afterwards. Captured 2026-10-12
// against scripts/mk-bus.py; the same command with `--json` exits 2,
// "unrecognized arguments: --json".
export const PENDING_CAPTURED = '{"ok":true,"run":"*","pending":[{"seq":1,"id":"m-1","ts":"2026-10-12T12:23:53Z","run":"r1","task":"T1","from":"T1","to":"orch","kind":"ask","reply_to":null,"text":"how to proceed?","ack_level":1}]}'
