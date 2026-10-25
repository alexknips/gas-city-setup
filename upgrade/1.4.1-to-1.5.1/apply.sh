#!/usr/bin/env bash
# The upgrade: gc 1.4.1 -> 1.5.1 and bd 1.2.2 -> 1.3.2 on this town, in the order the 1.5.1 notes require
# (bd first, then the supervisor on the new gc, then `gc doctor --fix` once). Run it detached, never from a pane:
#     /home/ubuntu/gc-town-upgrade/run-detached.sh apply.sh        (see RUNBOOK.md)
# The town is DOWN from step 3 to step 7. Anything that fails after step 3 stops here with the town stopped or
# half-upgraded and prints how to back out: rollback.sh restores the cold backup step 4 took.
#
#   1  precheck (load, no refinery mid-merge, binaries installed, disk)             town up
#   2  before-snapshot (config, orders, status, ...) for the diff                   town up
#   3  gc stop, stop the supervisor unit, stop the city's Dolt                      DOWN
#   4  cold backup of Dolt data, config, ~/.gc, unit, wrappers
#   5  repoint ~/.local/bin/{gc,bd} and the unit's ExecStart to the 1.5.1 / 1.3.2 installs
#   6  bd migrate schema per scope (53 -> 66), start the supervisor, register + start the city on 1.5.1
#   7  gc doctor --fix, wait for the city to be ready                               UP
#   8  after-snapshot + postcheck.sh
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DOWN=0
T0=$(date +%s)
t() { printf '%dm%02ds' $(( ($(date +%s) - T0) / 60 )) $(( ($(date +%s) - T0) % 60 )); }
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$DOWN" = 1 ]; then
    printf '\n%s APPLY FAILED (exit %s, %s in) with the town stopped or half-upgraded.\n' "$(ts)" "$rc" "$(t)"
    printf 'Back out:  %s/run-detached.sh rollback.sh      (restores %s)\n' "$UPG" "$(cat "$BACKUP_ROOT/latest" 2>/dev/null || echo '<no backup yet>')"
    printf 'Or read the log above, fix the cause and finish the remaining steps by hand.\n'
  fi
}
trap on_exit EXIT

step "0. apply gc $GC_VER + bd $BD_VER to $CITY  (load $(load1), log ${GCU_LOG:-stdout})"
[ -x "$GC_OLD" ] && [ -x "$BD_OLD" ] || die "the old binaries are missing: $GC_OLD $BD_OLD"
[ "$("$GC_OLD" version 2>&1)" = 1.4.1 ] || die "the old gc is not 1.4.1"

step "1. precheck (and install, which is idempotent and leaves everything running)"
"$UPG/install.sh"
if [ "${GCU_SKIP_PRECHECK:-0}" = 1 ]; then log "GCU_SKIP_PRECHECK=1: skipped"; else "$UPG/precheck.sh" || die "precheck failed; nothing was changed"; fi

step "2. before-snapshot (town still up)"
CAP=$BACKUP_ROOT/capture-$(date -u +%Y%m%dT%H%M%SZ)
"$UPG/capture.sh" "$CAP-before" --quick || log "capture-before had errors (only used for the before/after diff)"
log "snapshot: $CAP-before"

step "3. stop the town"
DOWN=1
t_down=$(date +%s)
rc=0; gcrun timeout 600 "$GC_OLD" stop || rc=$?
log "gc stop exit $rc"
systemctl --user stop "$UNIT_NAME"
stop_city_dolt
if tmux -L gc ls >/dev/null 2>&1; then
  log "sessions survived gc stop; killing the city's tmux server (socket -L gc)"
  tmux -L gc kill-server || true
fi
systemctl --user is-active --quiet "$UNIT_NAME" && die "$UNIT_NAME is still active"
! pgrep -af 'dolt sql-server' | grep -q "$CITY/" || die "the city's dolt is still running"
! tmux -L gc ls >/dev/null 2>&1 || die "tmux -L gc still has sessions"
log "town is down ($(t))"

step "4. cold backup"
"$UPG/backup.sh"
log "backup: $(cat "$BACKUP_ROOT/latest")"

step "5. repoint wrappers and unit to gc $GC_VER / bd $BD_VER"
repoint() {  # repoint <file> <old-exec-prefix> <new-binary>
  local f=$1 old=$2 new=$3
  [ "$(grep -c "^exec $old " "$f")" = 1 ] || die "$f: expected exactly one 'exec $old' line"
  sed -i "s|^exec $old |exec $new |" "$f"
  [ "$(grep -c "^exec $new " "$f")" = 1 ] || die "$f: repoint to $new failed"
}
repoint "$WRAP_GC" "$BREW/bin/gc" "$GC_NEW"
repoint "$WRAP_BD" "$BREW/bin/bd" "$BD_NEW"
grep -q "^ExecStart=$GC_OLD supervisor run\$" "$UNIT_FILE" || die "unit ExecStart is not the expected old line"
sed -i "s|^ExecStart=$GC_OLD supervisor run\$|ExecStart=$GC_NEW supervisor run|" "$UNIT_FILE"
grep -q "^ExecStart=$GC_NEW supervisor run\$" "$UNIT_FILE" || die "unit repoint failed"
systemctl --user daemon-reload
log "wrappers: $(grep '^exec ' "$WRAP_GC") / $(grep -m1 '^exec /' "$WRAP_BD")"
log "unit:     $(grep '^ExecStart=' "$UNIT_FILE")"
[ "$("$WRAP_GC" version)" = "$GC_VER" ] || die "gc via the wrapper is not $GC_VER"

step "6. bd schema migration, then start on gc $GC_VER"
migrate_schema "$GC_NEW"
systemctl --user start "$UNIT_NAME"
wait_supervisor "$GC_NEW"
start_city "$GC_NEW"

step "7. gc doctor --fix (once), then wait for the city"
gcrun timeout 1800 "$GC_NEW" doctor --fix || log "gc doctor --fix exit $? (findings are in the output above; postcheck decides)"
wait_city_ready "$GC_NEW"
DOWN=0
log "town is up on gc $GC_VER after $(t) in total, $(( $(date +%s) - t_down ))s of it down"

step "8. after-snapshot and postcheck"
"$UPG/capture.sh" "$CAP-after" --quick || log "capture-after had errors"
"$UPG/postcheck.sh"
step "apply complete ($(t)); snapshots $CAP-before and $CAP-after; backup $(cat "$BACKUP_ROOT/latest")"
