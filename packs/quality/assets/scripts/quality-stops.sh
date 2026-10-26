#!/usr/bin/env bash
# Entry point of the quality-stops order: the stop census (stops.py) at low CPU and IO priority.
# The load gate is inside stops.py: above QUALITY_MAX_LOAD it records only the host part. Every
# run appends one line to $QUALITY_STATE_DIR/stops.log.
#   quality-stops.sh               the census (what the order runs)
#   quality-stops.sh --dry-run     print it; write nothing
#   quality-stops.sh --bead ID     how one bead was classified, with its inputs
set -u
city=${GC_CITY_PATH:-${GC_CITY:-$PWD}}
state=${QUALITY_STATE_DIR:-$city/.gc/runtime/quality-cache}
here=$(cd "$(dirname "$0")" && pwd)
# The metrics order records where it writes; use the same place unless this order says otherwise.
if [ -z "${QUALITY_OUT_DIR:-}" ] && [ -s "$state/out-dir" ]; then
  QUALITY_OUT_DIR=$(cat "$state/out-dir")
  export QUALITY_OUT_DIR
fi
prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
exec "${prio[@]}" python3 "$here/stops.py" "$@"
