#!/usr/bin/env bash
# Run every scripts/*.py and child-skills/*/scripts/*.py that has a --self-test; exit non-zero if any fails.
# mk-bus.py --self-test runs as part of the *.py pattern below.
# Tests use temp dirs and a temp MATRIXKODER_HOME, never the real ~/.matrixkoder.
cd "$(dirname "$0")" || exit 2
export PYTHONDONTWRITEBYTECODE=1
# The self-tests play the orchestrator (merge, release, branch -D), so they need the real git:
# drop a lane's git guard (<lane>/.matrixkoder-bin) from PATH when run inside a lane.
PATH=$(tr ':' '\n' <<<"$PATH" | grep -v '/\.matrixkoder-bin/*$' | paste -sd: -)
export PATH
fail=0 ran=0
for f in *.py ../child-skills/*/scripts/*.py; do
  [ -e "$f" ] || continue
  grep -q -- '--self-test' "$f" || { echo "skip $f (no --self-test)"; continue; }
  ran=$((ran + 1))
  if out=$(python3 "$f" --self-test 2>&1); then
    echo "pass $f: $(tail -n 1 <<<"$out")"
  else
    echo "FAIL $f"; sed 's/^/  /' <<<"$out"; fail=$((fail + 1))
  fi
done
echo "selftest: $ran run, $fail failed"
[ "$fail" -eq 0 ]
