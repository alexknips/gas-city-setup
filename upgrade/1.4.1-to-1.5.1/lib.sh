# shellcheck shell=bash
# Shared settings and helpers for the gc 1.4.1 -> 1.5.1 upgrade scripts (source it, do not run it).
# Absolute paths only: the apply runs detached under `systemd-run --user`, where PATH and cwd are not the
# interactive shell's. Every path can be overridden with GCU_* for the rehearsal box; the defaults are the live town.

export HOME=${HOME:-/home/ubuntu}
CITY=${GCU_CITY:-/home/ubuntu/gc}
GCHOME=${GC_HOME:-$HOME/.gc}
BREW=${GCU_BREW:-/home/linuxbrew/.linuxbrew}
UPG=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# What runs today: brew's gc 1.4.1 and bd 1.2.2 (the unit's ExecStart and both ~/.local/bin wrappers point there)
GC_OLD=$BREW/Cellar/gascity/1.4.1/bin/gc
BD_OLD=$BREW/Cellar/beads/1.2.2/bin/bd

# What it becomes: release tarballs unpacked into versioned dirs next to, not over, the brew install
GC_VER=1.5.1
BD_VER=1.3.2
OPT=${GCU_OPT:-$HOME/.local/opt}
GC_NEW=$OPT/gascity/$GC_VER/gc
BD_NEW=$OPT/beads/$BD_VER/bd

WRAP_GC=$HOME/.local/bin/gc
WRAP_BD=$HOME/.local/bin/bd
UNIT_NAME=gascity-supervisor
UNIT_FILE=$HOME/.local/share/systemd/user/$UNIT_NAME.service
BACKUP_ROOT=${GCU_BACKUP_ROOT:-$HOME/gc-upgrade-backups}
LOAD_MAX=${GCU_LOAD_MAX:-20}   # the town's rule: stop above load 20

# The unit's own PATH, minus nothing: the wrappers in ~/.local/bin come first, as in the supervisor.
export PATH=$HOME/.local/bin:$BREW/bin:/usr/local/bin:/usr/bin:/bin
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
export DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}

ts()   { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()  { printf '%s %s\n' "$(ts)" "$*"; }
step() { printf '\n%s ==> %s\n' "$(ts)" "$*"; }
die()  { printf '%s FAIL: %s\n' "$(ts)" "$*" >&2; exit 1; }
load1() { cut -d' ' -f1 /proc/loadavg; }
load_ok() { awk -v l="$(load1)" -v m="$LOAD_MAX" 'BEGIN{exit !(l<=m)}'; }

# Newest backup dir written by backup.sh (a path in $BACKUP_ROOT/latest), or the one passed in GCU_BACKUP.
backup_dir() {
  if [ -n "${GCU_BACKUP:-}" ]; then printf '%s\n' "$GCU_BACKUP"; return; fi
  [ -f "$BACKUP_ROOT/latest" ] || die "no $BACKUP_ROOT/latest: run backup.sh first (or set GCU_BACKUP)"
  cat "$BACKUP_ROOT/latest"
}

# Stop the city's own managed Dolt if it is up. Any gc/bd store call after `gc stop` (even `gc status`) starts it again,
# so run this last, right before the cold backup. SIGTERM is a clean Dolt shutdown (<2s measured); its scope watchdog
# exits with it. Only the process named in the city's own dolt.pid, and only if its command line is the city's config.
stop_city_dolt() {
  local pidf=$CITY/.gc/runtime/packs/dolt/dolt.pid pid i
  [ -f "$pidf" ] || return 0
  pid=$(cat "$pidf")
  { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; } || return 0
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "dolt sql-server --config $CITY/" \
    || die "pid $pid from $pidf is not the city's dolt sql-server: not touching it"
  log "stopping the city's dolt (pid $pid)"
  kill -TERM "$pid"
  for i in $(seq 90); do
    kill -0 "$pid" 2>/dev/null || { log "dolt gone after ${i}s"; return 0; }
    sleep 1
  done
  die "dolt pid $pid still running 90s after SIGTERM"
}

gcrun() { ( cd "$CITY" && "$@" ); }   # gc is path-sensitive: run from the city dir

# wait_supervisor <gc>: the supervisor unit answers on its API
wait_supervisor() {
  local gc=$1 i
  for i in $(seq 60); do
    gcrun timeout 20 "$gc" supervisor status >/dev/null 2>&1 && { log "supervisor answers (${i} tries)"; return 0; }
    sleep 2
  done
  die "supervisor not answering after 120s: journalctl --user -u $UNIT_NAME -n 50 --no-pager"
}

# start_city <gc>: (re)register the city with the supervisor and start it. `gc stop` unregisters it, so this is needed
# after every stop. 1.5.1's gc refuses a supervisor that runs a different gc install: the unit must be repointed first.
start_city() { log "$1 start"; gcrun timeout 900 "$1" start; }

# wait_city_ready <gc>: until gc status shows the controller and no 'partial status' (agents it could not probe yet)
wait_city_ready() {
  local gc=$1 t=${GCU_READY_TIMEOUT:-900} t0 st=
  t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt "$t" ]; do
    st=$(gcrun timeout 120 "$gc" status 2>&1) || true
    if grep -q 'Controller: *supervisor-managed' <<<"$st" && ! grep -q 'partial status' <<<"$st"; then
      log "city ready after $(( $(date +%s) - t0 ))s"; return 0
    fi
    sleep 15
  done
  die "city not ready after ${t}s; last status: $(head -4 <<<"$st" | tr '\n' '|')"
}

# The databases of the city's managed Dolt server: one dir per database under .beads/dolt
dolt_dbs() { find "$CITY/.beads/dolt" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' | sort; }
