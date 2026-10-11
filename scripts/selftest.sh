#!/usr/bin/env bash
# Run every scripts/*.py and child-skills/*/scripts/*.py that has a --self-test; exit non-zero if any fails.
# mk-bus.py --self-test runs as part of the *.py pattern below.
# Tests use temp dirs and a temp MATRIXKODER_HOME, never the real ~/.matrixkoder.
cd "$(dirname "$0")" || exit 2
export PYTHONDONTWRITEBYTECODE=1
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
