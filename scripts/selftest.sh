#!/usr/bin/env bash
# Run every scripts/*.py and child-skills/*/scripts/*.py that has a --self-test; exit non-zero if any fails.
# mk-bus.py --self-test runs as part of the *.py pattern below.
# Tests use temp dirs and one temp MK home for the whole run, never the real ~/.matrixkoder.
# Jobs: MK_SELFTEST_JOBS when set (a non-number or a value < 1 warns and runs serially); otherwise
# session.selftest_jobs_total from one `python3 mk-config.py get` in its own process group,
# killed after 5 s (a missing yaml, a missing script, a timeout, a non-zero exit or any other
# failure falls back to 4; only a timeout prints one stderr line; a saved value that is not an
# integer from 1 to 16 warns once and uses 4); otherwise 4.
# N runs N self-tests at once, the slow ones first; each one's output is buffered and printed after
# the run in discovery order, so the report and the last line match a serial run.
cd "$(dirname "$0")" || exit 2
export PYTHONDONTWRITEBYTECODE=1
# The self-tests play the orchestrator (merge, release, branch -D), so they need the real git:
# drop a lane's git guard (<lane>/.matrixkoder-bin) from PATH when run inside a lane.
PATH=$(tr ':' '\n' <<<"$PATH" | grep -v '/\.matrixkoder-bin/*$' | paste -sd: -)
export PATH
# R1.1 (C16): one temp MK home under both names for the whole run, plus the
# write-guard flag. Every suite inherits it; each --self-test pins its own temp
# home under the same names. $MK_BUS_DIR is NOT set here: a file-mode bus keeps
# bus.json in its data dir, and one MK_BUS_DIR is one bus for every data dir.
# Removed after the run.
mk_home=$(mktemp -d "${TMPDIR:-/tmp}/mk-selftest-home.XXXXXX") || exit 2
export MK_HOME="$mk_home" MATRIXKODER_HOME="$mk_home" MK_SELFTEST=1
cleanup_home() { rm -rf "$mk_home"; }
if [ -n "${MK_SELFTEST_JOBS:-}" ]; then
  jobs=$MK_SELFTEST_JOBS
  case $jobs in *[!0-9]*) jobs=0 ;; esac
  if [ "$jobs" -lt 1 ]; then
    echo "warning: MK_SELFTEST_JOBS=${MK_SELFTEST_JOBS} is not a number >= 1, running serially" >&2
    jobs=1
  fi
else
  jobs=4
  cfg_py=$(dirname "$0")/mk-config.py
  if [ -f "$cfg_py" ]; then
    # Bound the lookup: a hung or blocked mk-config.py must not stall every lane's suite.
    # macOS has no GNU timeout and bash 3.2 has no wait -n, so poll for 5 s then kill.
    cfg_out=$(mktemp "${TMPDIR:-/tmp}/mk-selftest-cfg.XXXXXX") || exit 2
    # Own process group so the 5 s kill reaches python, not only the waiting shell.
    # macOS has no GNU timeout and bash 3.2 has no wait -n. The lookup line is unchanged.
    set -m
    { cfg=$(python3 "$cfg_py" get session.selftest_jobs_total 2>/dev/null) || cfg=
      printf '%s' "$cfg" >"$cfg_out"; } &
    cfg_pid=$!
    cfg_i=0
    # The shell exits once python starts, so wait on the output file, not on the shell.
    while [ ! -s "$cfg_out" ] && kill -0 -"$cfg_pid" 2>/dev/null && [ "$cfg_i" -lt 50 ]; do sleep 0.1; cfg_i=$((cfg_i + 1)); done
    if [ ! -s "$cfg_out" ] && kill -0 -"$cfg_pid" 2>/dev/null; then
      kill -KILL -"$cfg_pid" 2>/dev/null
      wait "$cfg_pid" 2>/dev/null
      echo "warning: session.selftest_jobs_total lookup timed out after 5s, using 4" >&2
      cfg=
    else
      wait "$cfg_pid" 2>/dev/null
      cfg=$(cat "$cfg_out" 2>/dev/null) || cfg=
    fi
    set +m
    rm -f "$cfg_out"
    got=$(printf '%s' "$cfg" | python3 -c 'import json,sys
try:
    o=json.loads(sys.stdin.read())
    v=o.get("value")
    raw=o.get("raw")
    derived=o.get("derived") is True
except Exception:
    v=None
    raw=None
    derived=False
if raw is not None:
    print("BAD:"+str(raw))
elif (not derived) and isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 16:
    print(v)
' 2>/dev/null) || got=
    case $got in
      ''|*[!0-9]*)
        case $got in
          BAD:*) echo "warning: session.selftest_jobs_total=${got#BAD:} is not an integer from 1 to 16, using 4" >&2 ;;
        esac
        ;;
      *) jobs=$got ;;
    esac
  fi
fi
if [ -n "${MK_SELFTEST_TRACE:-}" ]; then printf '%s\n' "$jobs" >"$MK_SELFTEST_TRACE"; fi
files=()
for f in *.py ../child-skills/*/scripts/*.py; do
  [ -e "$f" ] || continue
  files+=("$f")
done
if [ "$jobs" -gt 1 ]; then
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/mk-selftest.XXXXXX") || exit 2
  export tmp
  xp=
  # Ctrl-C / SIGTERM: stop xargs and every test under it (its own process group), then drop the buffers.
  cleanup() { [ -n "$xp" ] && { kill -TERM -- "-$xp"; wait "$xp"; } 2>/dev/null; rm -rf "$tmp"; cleanup_home; }
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
else
  trap cleanup_home EXIT
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
# C1: no MK file name is spelled outside mkpaths.py / hooks/paths.ts. The kept hits (message text, labels,
# fixtures) are listed in c1-gate.py, so the baseline is 0. Skipped when the gate is not beside this script.
c1_py=./c1-gate.py  # the script cd'd into its own dir above
if [ -f "$c1_py" ]; then
  ran=$((ran + 1))
  if out=$(python3 "$c1_py" check --baseline 0 2>&1); then
    echo "pass c1-gate check: $(tail -n 1 <<<"$out")"
  else
    echo "FAIL c1-gate check"; sed 's/^/  /' <<<"$out"; fail=$((fail + 1))
  fi
fi
echo "selftest: $ran run, $fail failed"
[ "$fail" -eq 0 ]
