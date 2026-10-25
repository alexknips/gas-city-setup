#!/usr/bin/env bash
# keeper.sh: keep the gcs78y pilot box up unattended (mayor ruling 2026-10-25: "restart the box and keep it up").
# Runs on the host. The watchdog kills the box on rules a-f and writes data/last-kill.txt; this restarts it only when
#   - the kill was load-class: rule d, e or f (a box that burned its CPU budget or ran into a busy host), and
#   - the host has been quiet (load1 < KEEPER_QUIET) for 3 checks in a row and KEEPER_COOLDOWN s have passed, and
#   - fewer than KEEPER_MAX restarts happened.
# A kill by rule a, b or c (herdr count, zombies, pids) is the runaway the guardrail exists for: the keeper stops
# and writes data/NEEDS-HUMAN. A box stopped by hand (no last-kill.txt) is left alone.
# Every decision goes to data/keeper.log.
set -u
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
data=$here/data
pilot=$here/pilot.sh
name=${PILOT_NAME:-gcs78y-pilot}
quiet=${KEEPER_QUIET:-12}
cooldown=${KEEPER_COOLDOWN:-300}
max=${KEEPER_MAX:-12}
city=/home/ubuntu/scratch-city
restarts=0 calm=0

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$data/keeper.log"; }
box() { docker exec -u ubuntu -e HOME=/home/ubuntu -w /home/ubuntu "$name" "$@"; }

log "keeper start (quiet<$quiet cooldown=${cooldown}s max=$max)"
while :; do
  sleep 30
  running=$(timeout 20 docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo unknown)
  [ "$running" = false ] || { calm=0; continue; }          # running, or docker unresponsive: nothing to do
  [ -s "$data/last-kill.txt" ] || continue                  # stopped by hand
  IFS=$'\t' read -r kts rules _ < "$data/last-kill.txt"
  case $rules in
    *a:*|*b:*|*c:*) log "NEEDS-HUMAN: runaway rule tripped ($rules); not restarting"; echo "$rules" > "$data/NEEDS-HUMAN"; exit 2 ;;
  esac
  load=$(cut -d' ' -f1 /proc/loadavg)
  if awk -v l="$load" -v q="$quiet" 'BEGIN{exit !(l<q)}'; then calm=$((calm+1)); else calm=0; fi
  [ "$calm" -ge 3 ] && [ $(( $(date +%s) - kts )) -ge "$cooldown" ] || continue
  if [ "$restarts" -ge "$max" ]; then log "restart cap $max reached; keeper ends"; exit 2; fi
  restarts=$((restarts+1)); calm=0
  log "restart #$restarts after kill [$rules] (load1 $load)"
  "$pilot" up >> "$data/keeper.log" 2>&1 || { log "pilot.sh up failed"; continue; }
  rm -f "$data/last-kill.txt"
  (cd "$here/../.." && nohup "$pilot" watch "$data/samples.tsv" --every 30 >> "$data/watchdog.log" 2>&1 &)
  box gc supervisor start >> "$data/keeper.log" 2>&1
  box gc start "$city" >> "$data/keeper.log" 2>&1
  log "restart #$restarts done"
done
