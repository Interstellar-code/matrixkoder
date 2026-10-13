// Captured mkdb reply, generated from plans-list.json. Do not hand-edit.
const data = {
  "ok": true,
  "plans": [
    {
      "id": "settings-plans-db",
      "title": "Settings field mapping, plans in the DB, and Plan tab sub-tabs",
      "status": "running",
      "created": "2026-10-13",
      "design": [
        ".matrixkoder/plans/settings-tab.md",
        ".matrixkoder/plans/settings-wire.md",
        ".matrixkoder/plans/dashboard-v2.md",
        ".matrixkoder/plans/mk-store.md"
      ],
      "integration": "main",
      "version": 5,
      "approved_at": "2026-10-13T21:30:56+02:00",
      "runs": [
        "settings-plans-db-r1"
      ],
      "live_run": "settings-plans-db-r1",
      "counts": {
        "merged": 8,
        "done": 2,
        "planned": 3,
        "building": 1
      }
    }
  ]
} as const
export default data
