#!/usr/bin/env bash
# Entry point of the quality-metrics order: skip when the host is busy, else run quality.py at
# low CPU and IO priority. Every run (or skip) appends one line to $QUALITY_STATE_DIR/runs.log.
#   quality-metrics.sh             compute and write (what the order runs)
#   quality-metrics.sh --dry-run   compute and print a summary; write nothing
#   quality-metrics.sh --bead ID   show how one landed bead was counted
set -u
city=${GC_CITY_PATH:-${GC_CITY:-$PWD}}
state=${QUALITY_STATE_DIR:-$city/.gc/runtime/quality-cache}
max=${QUALITY_MAX_LOAD:-15}
here=$(cd "$(dirname "$0")" && pwd)
[ $# -eq 0 ] && mkdir -p "$state" 2>/dev/null
note() {  # the order's runs only: --dry-run and --bead write nothing
  [ $# -gt 0 ] && [ -z "$args" ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$state/runs.log" 2>/dev/null
}
args="$*"

load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || uptime | sed 's/.*average[s]*: //; s/,.*//')
if awk -v l="$load" -v m="$max" 'BEGIN { exit !(l + 0 > m + 0) }'; then
  echo "quality-metrics: skipped: load $load > $max"
  note "skipped load=$load max=$max args=$*"
  exit 0
fi

prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
start=$(date +%s)
"${prio[@]}" python3 "$here/quality.py" "$@"
rc=$?
note "rc=$rc load=$load secs=$(($(date +%s) - start)) args=$*"
exit $rc
