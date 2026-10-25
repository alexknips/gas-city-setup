#!/usr/bin/env bash
# Launch apply.sh or rollback.sh as a transient `systemd-run --user` unit, detached from the caller.
#
# Why: the apply stops every agent session, and the session that runs this (the mayor's tmux pane, a child of the
# supervisor) is one of them. A plain child process would die at `gc stop`, halfway through the upgrade. A transient
# unit has its own cgroup, so neither `gc stop` nor stopping gascity-supervisor.service touches it, and it keeps going
# after the caller is gone. Everything is absolute: cwd is the city, PATH is set explicitly, output goes to a log file.
#
#   run-detached.sh apply.sh [args]       start, print the unit and log names, return at once
#   run-detached.sh rollback.sh [args]
#   run-detached.sh --foreground apply.sh [args]   same but wait for the unit and exit with its status (--wait)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

mode=--no-block
if [ "${1:-}" = --foreground ]; then mode=--wait; shift; fi
script=${1:?usage: run-detached.sh [--foreground] apply.sh|rollback.sh [args]}; shift
case $script in apply.sh|rollback.sh) ;; *) die "only apply.sh and rollback.sh are launched detached, not '$script'" ;; esac
[ -x "$UPG/$script" ] || die "$UPG/$script is not executable"

# one upgrade/rollback at a time
if systemctl --user list-units --state=active,activating --plain --no-legend 'gcu-apply-*' 'gcu-rollback-*' | grep -q .; then
  die "another gcu-apply/gcu-rollback unit is still running: systemctl --user list-units 'gcu-*'"
fi

mkdir -p "$BACKUP_ROOT"
unit=gcu-${script%.sh}-$(date -u +%Y%m%dT%H%M%SZ)
logf=$BACKUP_ROOT/$unit.log
: >> "$logf"

args=(--user --unit="$unit" --collect "$mode"
  --working-directory="$CITY"
  --setenv=HOME="$HOME" --setenv=PATH="$PATH" --setenv=XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"
  --setenv=DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" --setenv=LANG=C.UTF-8
  --setenv=GCU_LOG="$logf" --setenv=GCU_UNIT="$unit"
  --property=StandardOutput=append:"$logf" --property=StandardError=append:"$logf")
# pass the test-box overrides through, if any are set
for v in GCU_CITY GCU_BREW GCU_OPT GCU_BACKUP_ROOT GCU_BACKUP GCU_LOAD_MAX GC_HOME GCU_SKIP_PRECHECK GCU_READY_TIMEOUT; do
  [ -z "${!v:-}" ] || args+=(--setenv="$v=${!v}")
done

log "launching $script as $unit.service"
rc=0
systemd-run "${args[@]}" /usr/bin/env bash "$UPG/$script" "$@" || rc=$?
echo
echo "unit:   $unit.service"
echo "log:    $logf"
echo "follow: tail -f $logf"
echo "state:  systemctl --user status $unit --no-pager   (a finished unit is collected; the log is the record)"
exit "$rc"
