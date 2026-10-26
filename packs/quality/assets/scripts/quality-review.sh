#!/usr/bin/env bash
# Entry point of the quality-review order. Fires hourly; once a week, after QUALITY_REVIEW_WEEKDAY at
# QUALITY_REVIEW_HOUR (local) and after that day's quality-metrics run, it writes the week's facts
# (quality_review.py: no model) and, when QUALITY_REVIEW_AGENT is set, slings the formula
# mol-quality-review to that agent, which proposes one config change by mail. Every run that does
# the work or skips for load appends one line to $QUALITY_STATE_DIR/runs.log.
#   quality-review.sh                  what the order runs
#   quality-review.sh --dry-run        print the facts for the due week; write nothing, sling nothing
#   quality-review.sh --week 2026-W43  write that week's facts (a backtest); no sling, no stamp
#   quality-review.sh --index          rebuild reviews/index.json for the page (the seat runs this)
#   QUALITY_FORCE=1 gc order run quality-review   run now although this week's review is done
set -u
city=${GC_CITY_PATH:-${GC_CITY:-$PWD}}
state=${QUALITY_STATE_DIR:-$city/.gc/runtime/quality-cache}
max=${QUALITY_MAX_LOAD:-15}
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gc-bin.sh"
gc=$GC_BIN
args="$*"
# The metrics order records where it writes; use the same place unless this order says otherwise.
if [ -z "${QUALITY_OUT_DIR:-}" ] && [ -s "$state/out-dir" ]; then
  QUALITY_OUT_DIR=$(cat "$state/out-dir")
  export QUALITY_OUT_DIR
fi
note() {  # the order's own runs only
  [ -z "$args" ] && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) review $*" >>"$state/runs.log" 2>/dev/null
}

case "${1:-}" in
  --index|--dry-run) exec python3 "$here/quality_review.py" "$@" ;;
  --week) exec nice -n 10 python3 "$here/quality_review.py" --facts "$@" ;;
  "") ;;
  *) echo "usage: quality-review.sh [--dry-run | --week YYYY-Www | --index]" >&2; exit 2 ;;
esac

week=$(python3 "$here/quality_review.py" --due) || { echo "$week"; exit 0; }

load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || uptime | sed 's/.*average[s]*: //; s/,.*//')
if awk -v l="$load" -v m="$max" 'BEGIN { exit !(l + 0 > m + 0) }'; then
  echo "quality-review: skipped: load $load > $max"
  note "skipped week=$week load=$load max=$max"
  exit 0
fi

start=$(date +%s)
out=$(nice -n 10 python3 "$here/quality_review.py" --facts)
rc=$?
if [ $rc -ne 0 ]; then
  note "week=$week rc=$rc load=$load secs=$(($(date +%s) - start)) facts failed"
  exit $rc
fi
IFS=$'\t' read -r week facts_md facts_json <<<"$out"
reviews=$(dirname "$facts_md")
echo "quality-review: facts for $week: $facts_md"

agent=${QUALITY_REVIEW_AGENT:-}
sling="none (QUALITY_REVIEW_AGENT empty: facts only)"
if [ -n "$agent" ]; then
  res=$("$gc" sling "$agent" mol-quality-review --formula --json --title "Quality review $week" \
    --var "week=$week" --var "facts=$facts_md" --var "facts_json=$facts_json" --var "reviews=$reviews" \
    --var "city=$city" --var "mail_to=${QUALITY_REVIEW_MAIL_TO:-mayor}" \
    --var "words=${QUALITY_REVIEW_WORDS:-300}" \
    --var "bead_cmd=$here/quality-metrics.sh --bead" --var "index_cmd=$here/quality-review.sh --index" 2>/dev/null)
  src=$?
  if [ $src -ne 0 ]; then
    echo "quality-review: gc sling to $agent failed (exit $src); facts are written, retrying next fire"
    note "week=$week rc=1 load=$load secs=$(($(date +%s) - start)) sling to $agent failed"
    exit 1
  fi
  # gc sling --json: molecule_id is the wisp's root, bead_id the work bead
  sling=$(printf '%s' "$res" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("molecule_id") or d.get("bead_id") or d.get("workflow_id") or "?")' 2>/dev/null || echo "?")
  echo "quality-review: slung mol-quality-review to $agent: $sling"
fi
echo "$week" >"$state/review-last-week" 2>/dev/null
note "week=$week rc=0 load=$load secs=$(($(date +%s) - start)) sling=$sling"
exit 0
