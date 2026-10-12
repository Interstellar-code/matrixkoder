#!/usr/bin/env bash
# Run every scripts/*.py and child-skills/*/scripts/*.py that has a --self-test; exit non-zero if any fails.
# mk-bus.py --self-test runs as part of the *.py pattern below.
# Tests use temp dirs and a temp MATRIXKODER_HOME, never the real ~/.matrixkoder.
# MK_SELFTEST_JOBS=N (default 4) runs N self-tests at once, the slow ones first; each one's output is
# buffered and printed after the run in discovery order, so the report and the last line match a serial
# run. MK_SELFTEST_JOBS=1, or a value that is not a number >= 1 (with a warning), runs them one by one.
cd "$(dirname "$0")" || exit 2
export PYTHONDONTWRITEBYTECODE=1
# The self-tests play the orchestrator (merge, release, branch -D), so they need the real git:
# drop a lane's git guard (<lane>/.matrixkoder-bin) from PATH when run inside a lane.
PATH=$(tr ':' '\n' <<<"$PATH" | grep -v '/\.matrixkoder-bin/*$' | paste -sd: -)
export PATH
jobs=${MK_SELFTEST_JOBS:-4}
case $jobs in *[!0-9]*) jobs=0 ;; esac
if [ "$jobs" -lt 1 ]; then
  echo "warning: MK_SELFTEST_JOBS=${MK_SELFTEST_JOBS} is not a number >= 1, running serially" >&2
  jobs=1
fi
files=()
for f in *.py ../child-skills/*/scripts/*.py; do
  [ -e "$f" ] && files+=("$f")
done
if [ "$jobs" -gt 1 ]; then
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/mk-selftest.XXXXXX") || exit 2
  export tmp
  xp=
  # Ctrl-C / SIGTERM: stop xargs and every test under it (its own process group), then drop the buffers.
  cleanup() { [ -n "$xp" ] && { kill -TERM -- "-$xp"; wait "$xp"; } 2>/dev/null; rm -rf "$tmp"; }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  list=()
  for f in "${files[@]}"; do
    grep -q -- '--self-test' "$f" && list+=("$f")
  done
  # slowest first so the longest test starts at once; the rest in discovery order
  order=()
  for h in mk-lane-event mk-config mk-lane git_guard_test mk-state lane-watch mk-bus mk-builder mk-plan; do
    for f in "${list[@]}"; do [ "$(basename "$f" .py)" = "$h" ] && order+=("$f"); done
  done
  for f in "${list[@]}"; do
    case " ${order[*]} " in *" $f "*) ;; *) order+=("$f") ;; esac
  done
  set -m  # xargs in its own process group, so cleanup can signal the whole tree
  printf '%s\0' "${order[@]}" >"$tmp/list"
  xargs -0 -P "$jobs" -I{} bash -c \
    'k=${1//\//_}; python3 "$1" --self-test >"$tmp/$k.out" 2>&1; echo $? >"$tmp/$k.rc"' _ {} <"$tmp/list" &
  xp=$!
  wait "$xp"
  xp=
fi
fail=0 ran=0
for f in "${files[@]}"; do
  grep -q -- '--self-test' "$f" || { echo "skip $f (no --self-test)"; continue; }
  ran=$((ran + 1))
  if [ "$jobs" -gt 1 ]; then
    k=${f//\//_}
    out=$(cat "$tmp/$k.out" 2>/dev/null); rc=$(cat "$tmp/$k.rc" 2>/dev/null)
  else
    out=$(python3 "$f" --self-test 2>&1); rc=$?
  fi
  if [ "${rc:-1}" = 0 ]; then
    echo "pass $f: $(tail -n 1 <<<"$out")"
  else
    echo "FAIL $f"; sed 's/^/  /' <<<"$out"; fail=$((fail + 1))
  fi
done
echo "selftest: $ran run, $fail failed"
[ "$fail" -eq 0 ]
