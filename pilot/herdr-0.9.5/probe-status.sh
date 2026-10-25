#!/usr/bin/env bash
# probe-status.sh PANE SECONDS [TEXT]: optionally type TEXT and press Enter in PANE through herdr, then print one
# line (epoch, iso time, agent_status, terminal title) at every change of herdr's status for that pane for SECONDS.
# Measurement 2 of gcs-78y: how long until herdr reports `blocked` once a selector/question is parked.
# Env: PILOT_NAME (box), HERDR_SESSION (herdr --session name, default scratch78y).
set -euo pipefail
pane=${1:?pane id like w1:p3} secs=${2:?seconds} text=${3:-}
name=${PILOT_NAME:-gcs78y-pilot} sess=${HERDR_SESSION:-scratch78y}
hd() { docker exec -u ubuntu -e HOME=/home/ubuntu "$name" herdr --session "$sess" "$@"; }
status() {
  hd agent list | jq -r --arg p "$pane" '.result.agents[] | select(.pane_id==$p) | "\(.agent_status)\t\(.terminal_title_stripped)"'
}
t0=$(date +%s); last=
printf 'probe %s start %s (status before: %s)\n' "$pane" "$(date -u +%FT%TZ)" "$(status | cut -f1)"
if [ -n "$text" ]; then
  hd pane send-text "$pane" "$text" >/dev/null
  hd pane send-keys "$pane" enter >/dev/null
  printf 'typed %q + Enter at +%ss\n' "$text" "$(( $(date +%s) - t0 ))"
fi
while [ $(( $(date +%s) - t0 )) -lt "$secs" ]; do
  cur=$(status || echo "?")
  if [ "$cur" != "$last" ]; then
    printf '+%ss\t%s\t%s\n' "$(( $(date +%s) - t0 ))" "$(date -u +%T)" "$cur"
    last=$cur
  fi
  sleep 1
done
printf 'probe end %s\n' "$(date -u +%FT%TZ)"
