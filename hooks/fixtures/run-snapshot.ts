// Captured `mkdb.py run-snapshot` reply, generated from run-snapshot.json by
// scripts/snapshot-drift.py. Do not hand-edit; run `snapshot-drift.py --write`.
// The capture: a run written by scripts/mk-state.py in a temp data dir, read back
// through the production reader. Timestamps and absolute paths are pinned.
export const RUN_SNAPSHOT = {
  "active": "l5fix",
  "ok": true,
  "run": {
    "branch": "mk/l5",
    "ended": null,
    "feature": "L5 fixtures",
    "id": "l5fix",
    "next": "verify T1 gates",
    "plan": null,
    "plan_id": null,
    "plan_sha": null,
    "plan_status": null,
    "recent": [
      {
        "by": "lane-watch",
        "from": "planned",
        "run": "l5fix",
        "t": "<t>",
        "task": "P1",
        "text": "roll-up from children: planned → building",
        "to": "building",
        "type": "transition"
      },
      {
        "by": "lane-watch",
        "from": "dispatched",
        "run": "l5fix",
        "t": "<t>",
        "task": "T1",
        "text": "lane-watch",
        "to": "building",
        "type": "transition"
      },
      {
        "by": "lane-watch",
        "from": "building",
        "run": "l5fix",
        "t": "<t>",
        "task": "T1",
        "text": "building → merged",
        "to": "merged",
        "type": "transition"
      },
      {
        "by": "lane-watch",
        "from": null,
        "run": "l5fix",
        "t": "<t>",
        "task": "T2",
        "text": "deps met (T1): ready to dispatch",
        "to": null,
        "type": "note"
      },
      {
        "by": "lead",
        "from": "planned",
        "run": "l5fix",
        "t": "<t>",
        "task": "T2",
        "text": "picked up",
        "to": "building",
        "type": "transition"
      },
      {
        "by": "lead",
        "from": "planned",
        "run": "l5fix",
        "t": "<t>",
        "task": "T3",
        "text": "waiting on upstream",
        "to": "blocked",
        "type": "transition"
      },
      {
        "by": "lead",
        "from": null,
        "run": "l5fix",
        "t": "<t>",
        "task": "T4",
        "text": "queued",
        "to": null,
        "type": "note"
      },
      {
        "by": "orchestrator",
        "from": null,
        "run": "l5fix",
        "t": "<t>",
        "task": null,
        "text": "waiting on user: Merge T1 now?",
        "to": null,
        "type": "note"
      },
      {
        "by": "orchestrator",
        "from": null,
        "run": "l5fix",
        "t": "<t>",
        "task": null,
        "text": "fixture capture complete",
        "to": null,
        "type": "note"
      },
      {
        "by": "orchestrator",
        "from": null,
        "run": "l5fix",
        "t": "<t>",
        "task": null,
        "text": "stage 3",
        "to": null,
        "type": "note"
      }
    ],
    "schema": 1,
    "stage": 3,
    "started": "<started>",
    "status": "active",
    "tasks": [
      {
        "agent": null,
        "branch": null,
        "created": "<created>",
        "deps": [],
        "effort": null,
        "gates": null,
        "id": "P1",
        "lane": null,
        "last_commit": null,
        "model": null,
        "owned": [
          "scripts/p1.py"
        ],
        "pane": null,
        "parent": null,
        "ready": false,
        "round": 0,
        "rounds": [],
        "slot": null,
        "status": "building",
        "title": "Phase one",
        "updated": "<updated>",
        "verdict": null,
        "wave": 1,
        "workspace": null
      },
      {
        "agent": "claude",
        "branch": "mk/l5",
        "created": "<created>",
        "deps": [],
        "effort": "high",
        "gates": null,
        "id": "T1",
        "lane": "mk/l5",
        "last_commit": "1a2b3c4",
        "model": "opus",
        "owned": [
          "scripts/a.py"
        ],
        "pane": "w1P:pN",
        "parent": "P1",
        "ready": false,
        "round": 0,
        "rounds": [],
        "slot": "1",
        "status": "merged",
        "title": "First",
        "updated": "<updated>",
        "verdict": null,
        "wave": 1,
        "workspace": null
      },
      {
        "agent": "codex",
        "branch": null,
        "created": "<created>",
        "deps": [
          "T1"
        ],
        "effort": "low",
        "gates": null,
        "id": "T2",
        "lane": null,
        "last_commit": null,
        "model": "gpt",
        "owned": [
          "scripts/b.py",
          "scripts/c.py"
        ],
        "pane": null,
        "parent": "P1",
        "ready": false,
        "reviewer": "rex",
        "round": 0,
        "rounds": [],
        "slot": null,
        "status": "building",
        "title": "Second",
        "updated": "<updated>",
        "verdict": null,
        "wave": 2,
        "workspace": null
      },
      {
        "agent": null,
        "branch": null,
        "created": "<created>",
        "deps": [],
        "effort": null,
        "gates": null,
        "id": "T3",
        "lane": null,
        "last_commit": null,
        "model": null,
        "owned": [],
        "pane": null,
        "parent": null,
        "ready": false,
        "round": 0,
        "rounds": [],
        "slot": null,
        "status": "blocked",
        "title": "Third",
        "updated": "<updated>",
        "verdict": null,
        "wave": 1,
        "workspace": null
      },
      {
        "agent": null,
        "branch": null,
        "created": "<created>",
        "deps": [],
        "effort": null,
        "gates": null,
        "id": "T4",
        "lane": null,
        "last_commit": null,
        "model": null,
        "owned": [],
        "pane": null,
        "parent": null,
        "ready": false,
        "round": 0,
        "rounds": [],
        "slot": null,
        "status": "planned",
        "title": "Fourth",
        "updated": "<updated>",
        "verdict": null,
        "wave": 2,
        "workspace": null
      }
    ],
    "updated": "<updated>",
    "waiting": {
      "options": [
        "yes",
        "no"
      ],
      "question": "Merge T1 now?",
      "rec": "yes",
      "since": "<since>"
    }
  },
  "runs": [
    {
      "ended": null,
      "feature": "L5 fixtures",
      "id": "l5fix",
      "plan": null,
      "started": "<started>",
      "status": "active"
    }
  ],
  "waits": []
} as const
