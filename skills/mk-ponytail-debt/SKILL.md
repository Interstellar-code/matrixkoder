---
name: mk-ponytail-debt
description: List every `shortcut:` comment and `# ponytail:` limitation as a debt ledger. One-shot report, changes nothing. Use for "ponytail debt", "what did we defer", "list the shortcuts".
---

Every deliberate mk-ponytail shortcut is marked with a `shortcut:` comment naming its ceiling and upgrade path (older code uses `# ponytail:`). This collects them into one ledger so a deferral can't quietly become permanent.

## Scan

Grep the repo for both comment markers, skipping `node_modules`, `.git`, and build output:

`grep -rnE --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=build --exclude-dir=.ruff_cache '(#|//|/[*]) ?(shortcut|ponytail):' .`

Each hit is one ledger row. The comment prefix keeps prose that merely mentions the convention out of the ledger. Skip hits that are not a deferral, like a note about a keyboard shortcut.

## Output

One row per marker, grouped by file:

`<file>:<line>, <what was simplified>. ceiling: <the limit named>. upgrade: <the trigger to revisit>.`

The convention is `shortcut: <ceiling>, <upgrade path>` (or `ponytail: <ceiling>, <upgrade path>`). Pull the ceiling and the trigger straight from the comment.

Flag the rot risk: any marker comment that names no upgrade path or trigger gets a `no-trigger` tag; those are the ones that silently rot.

End with `<N> markers, <M> with no trigger.` Nothing found: `No shortcut debt. Clean ledger.`

## Boundaries

Reads and reports only, changes nothing. To persist it, ask and it writes the ledger to a file. One-shot.
