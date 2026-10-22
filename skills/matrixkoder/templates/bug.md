+++
# A bug draft — the file is <project>/bugs/<bug>.md. `mk-bug.py check <file>` checks it,
# `mk-bug.py import <file> [--dry-run]` creates bug B<n> in the project's permanent `bugs` run
# (its tasks B<n>.<m>, their subtasks B<n>.<m>.<k>) and moves the file to bugs/archive/B<n>-<file>.
# Every key lands in the bug row (tasks.extra_json key `bug`) unless it says otherwise.
# Only title is REQUIRED; a missing optional key takes the default shown.
bug = "crash-on-empty-plan"   # slug [a-z0-9][a-z0-9-]{0,40}, usually the file name; not stored
title = "mk-plan check crashes on an empty plan file"   # REQUIRED, one line — tasks.title
kind = "cli"                  # frontend|backend|cli|store|infra|docs|other, default other
severity = "med"              # high|med|low, default med
found_in = ""                 # version, commit or branch where it shows
area = ""                     # the part of the product, e.g. "plan check"
labels = []                   # list of strings, e.g. ["regression"]
source = ""                   # where the report came from: a todo, an issue link, a run id

[[task]]                      # optional, one block per task (B<n>.1, B<n>.2 …); a bug may start with none
title = "reproduce and fix the crash"   # REQUIRED in a task — tasks.title
acceptance = "mk-plan check on an empty file prints a check error, exit 1"   # advised — tasks.acceptance

[[task.subtask]]              # optional, under the [[task]] above it (B<n>.1.1 …); same keys as a task
title = "add a self-test case for the empty file"
acceptance = "the case fails before the fix and passes after"
+++

## Description

What is wrong, in two or three plain sentences. Imported as the bug's description.

## Steps

1. Create an empty plan.md.
2. Run `python3 scripts/mk-plan.py check plan.md`.

## Expected

A check error naming the missing header.

## Actual

A Python traceback.
