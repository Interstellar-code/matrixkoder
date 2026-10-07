# <PHASE> review (<reviewer agent/model>, <date>) — <APPROVE | REQUEST CHANGES>

Commit(s): <sha range>. Brief: <path>. Mockups: <paths>. Read-only review of the commit, not the working tree.

## CRITICAL / HIGH / MEDIUM / LOW
- `path:line` — problem — fix

## Must-check list (state OK or finding for each)
- Invented data: every status/label/count shown comes from an API field; unknown shows "—".
- Theming: no literal colours outside theme tokens; non-default themes render.
- Routes: every new client call has a real server handler, tested without mocking the handler.
- Overwrite / data loss: saves send the concurrency token; create paths can't overwrite.
- Shared components: other screens using them keep their defaults.
- Tests exercise real logic, not just mocks/snapshots.
- Accessibility basics: real buttons/labels, keyboard, no shortcuts firing while typing.

## Verified OK
<list>

## Not verified
<list>
