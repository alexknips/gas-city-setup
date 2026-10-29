#!/usr/bin/env bash
# Entry point of the quality-patrol-gate order (patrol_gate.py) at low CPU and IO priority.
#   quality-patrol-gate.sh             one run (what the order runs); nothing while QUALITY_GATE=off
#   quality-patrol-gate.sh --dry-run   print what a run would do; act on nothing, write nothing
set -u
city=${GC_CITY_PATH:-${GC_CITY:-$PWD}}
state=${QUALITY_STATE_DIR:-$city/.gc/runtime/quality-cache}
here=$(cd "$(dirname "$0")" && pwd)
case "${QUALITY_GATE:-off}:${1:-}" in
  off:|no:|0:|false:) exit 0 ;;
esac
. "$here/gc-bin.sh"
# A systemd PATH can leave out where the city's tmux lives; gc's own directory usually has it.
# gc needs it too: without tmux on PATH `gc session list` reports every session asleep.
if [ -z "${QUALITY_TMUX:-}" ] && ! command -v tmux >/dev/null 2>&1; then
  for t in "$(dirname "$GC_BIN")/tmux" "$(dirname "$(readlink -f "$GC_BIN" 2>/dev/null || echo "$GC_BIN")")/tmux"; do
    if [ -x "$t" ]; then QUALITY_TMUX=$t; export QUALITY_TMUX; break; fi
  done
fi
if [ -n "${QUALITY_TMUX:-}" ]; then PATH=$(dirname "$QUALITY_TMUX"):$PATH; export PATH; fi
# The census reads the metrics order's quality.json; use the place that order records.
if [ -z "${QUALITY_OUT_DIR:-}" ] && [ -s "$state/out-dir" ]; then
  QUALITY_OUT_DIR=$(cat "$state/out-dir")
  export QUALITY_OUT_DIR
fi
prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
exec "${prio[@]}" python3 "$here/patrol_gate.py" "$@"
