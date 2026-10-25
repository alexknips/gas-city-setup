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

# Capture, then match: `cmd | grep -q` / `cmd | head -1` under pipefail lets the early-exiting reader SIGPIPE the producer
# (rc 141), which kills a `set -e` script (install.sh did) or flips a `! cmd | grep -q` guard to "not running".
city_dolt_running() { local p; p=$(pgrep -af 'dolt sql-server' || true); [[ $p == *"$CITY/"* ]]; }
units_running() { local u; u=$(systemctl --user list-units --state=active,activating --plain --no-legend "$@" 2>/dev/null || true); [ -n "$u" ]; }

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
  local pidf=$CITY/.gc/runtime/packs/dolt/dolt.pid pid i cmdline
  [ -f "$pidf" ] || return 0
  pid=$(cat "$pidf")
  { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; } || return 0
  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  [[ $cmdline == *"dolt sql-server --config $CITY/"* ]] \
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

# wait_city_ready <gc>: the controller is supervisor-managed and at least $GCU_READY_SESSIONS (default 3) tmux sessions are up.
# Deliberately NOT "gc status has no 'partial status'": status marks every agent `unknown (partial status)` when its runtime
# probe times out (750 ms per agent) and that happens on a healthy city too: the rehearsal box shows 40 such lines on the
# 1.4.1 baseline and on 1.5.1, so that test never passes (the first rollback rehearsal waited 900 s on it). Nor on named seats:
# the box's mayor and deacon came and went while the reconciler retried their starts ("resuming session: context deadline
# exceeded"). This only says "the city started and is launching sessions"; postcheck.sh judges the rest.
wait_city_ready() {
  local gc=$1 t=${GCU_READY_TIMEOUT:-900} want=${GCU_READY_SESSIONS:-3} t0 st= n=0 ctl=no
  t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt "$t" ]; do
    st=$(gcrun timeout 120 "$gc" status 2>&1) || true
    ctl=no; grep -q 'Controller: *supervisor-managed' <<<"$st" && ctl=yes
    n=$(tmux -L gc ls 2>/dev/null | wc -l)
    if [ "$ctl" = yes ] && [ "$n" -ge "$want" ]; then
      log "city ready after $(( $(date +%s) - t0 ))s: controller up, $n tmux sessions, $(grep -c 'partial status' <<<"$st" || true) agent lines say 'partial status' (normal)"
      return 0
    fi
    sleep 15
  done
  die "city not ready after ${t}s (controller supervisor-managed: $ctl, tmux sessions: $n, want $want). It may only be slow: look at 'gc status', 'tmux -L gc ls' and $GCHOME/supervisor.log before deciding to roll back."
}

# migrate_schema <gc>: bd 1.3.x will not migrate a shared Dolt database by itself (#5920): without this, `gc start` on 1.5.1
# fails after ~80s with 'table "i" does not have column "storage_class"'. One `bd migrate schema` per scope (v53 -> v66,
# idempotent and resumable), run with the supervisor STOPPED: its init retries every 10s and hammer Dolt meanwhile.
# `gc bd` starts the city's managed Dolt itself. Rehearsal: 8 scopes took ~8 min on a quiet host (hivemind alone ~5).
# Databases bound to no rig ('mp') are not touched and stay at v53; 1.5.x no longer maintains them.
migrate_schema() {
  local gc=$1 s out
  while read -r s; do
    log "bd migrate schema in $s"
    out=$(cd "$s" && timeout 1800 "$gc" bd migrate schema 2>&1) \
      || { printf '%s\n' "$out" | tail -5; die "bd migrate schema failed in $s: run it again (resumable) or rollback.sh"; }
    log "  $(grep -E 'Schema|schema' <<<"$out" | tail -1)"
  done < <(city_scopes)
}

# repair_blocked <gc> <supervisor.log line count before the start>: on its first start 1.5.1 recomputes is_blocked per scope and
# skips a scope with uncommitted Dolt changes: "is_blocked repair for rig/X did not run ... (to repair by hand: bd recompute-blocked
# in <path>)". Rehearsal: neurofilament (a legacy rig). `gc bd dolt commit` is a no-op on a clean scope ("Nothing to commit.", rc 0).
repair_blocked() {
  local gc=$1 from=$2 s n=0
  while read -r s; do
    n=$((n + 1))
    log "is_blocked repair did not run in $s: committing the pending changes, then bd recompute-blocked"
    (cd "$s" && "$gc" bd dolt commit && timeout 900 "$gc" bd recompute-blocked) \
      || log "WARN: the repair in $s failed; by hand: cd $s && gc bd dolt commit && gc bd recompute-blocked"
  done < <(tail -n +"$((from + 1))" "$GCHOME/supervisor.log" 2>/dev/null \
            | sed -n 's/.*is_blocked repair for .* did not run.*bd recompute-blocked in \([^)]*\))$/\1/p' | sort -u)
  log "is_blocked repair: $n scope(s) needed it"
}

# The bd scopes: the city itself plus every rig path in .gc/site.toml. They all share the city's one Dolt server.
city_scopes() { { echo "$CITY"; sed -n 's/^path = "\(.*\)"/\1/p' "$CITY/.gc/site.toml"; } | sort -u; }

# The databases of the city's managed Dolt server: one dir per database under .beads/dolt
dolt_dbs() { find "$CITY/.beads/dolt" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' | sort; }
