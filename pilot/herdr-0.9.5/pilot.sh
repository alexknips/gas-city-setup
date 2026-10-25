#!/usr/bin/env bash
# Driver for the gcs-78y herdr pilot box. Subcommands:
#   build      stage the pinned binaries into a build context and build the image
#   up         start the box (detached) with resource limits and the kill-switch watchdog
#   exec ...   run a command in the box as the ubuntu user (no tty)
#   shell      interactive shell in the box (tty; for logins and the herdr TUI)
#   stats      one line: herdr procs, zombies, box load, host load
#   watch TSV  sample the box's cgroup into TSV every 30 s and enforce the kill rules (watchdog.py); run on the host
#   kill       stop the box at once (the kill switch)
#   down       stop and remove the box (the volume with the scratch city stays)
# Env: PILOT_NAME (box/volume name), PILOT_CPUS, PILOT_MEM.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
name=${PILOT_NAME:-gcs78y-pilot}
img=$name:latest
vol=$name-home
cpus=${PILOT_CPUS:-2}   # mayor ruling 2 (2026-10-25): B+cap, --cpus 2; --cpus 1 would switch rule e off by construction
mem=${PILOT_MEM:-12g}
stage=${PILOT_STAGE:-${TMPDIR:-/tmp}/$name-ctx}

# Pinned versions and where the host keeps them (versioned dirs from the 1.5.1 upgrade, see ../../upgrade)
GC_BIN=${PILOT_GC:-$HOME/.local/opt/gascity/1.5.1/gc}
BD_BIN=${PILOT_BD:-$HOME/.local/opt/beads/1.3.2/bd}
HERDR_VER=0.9.5
HERDR_SHA=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d   # herdr-linux-x86_64 of v0.9.5
DOLT_VER=2.3.2

log() { printf '[pilot] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
load1() { cut -d' ' -f1 /proc/loadavg; }

cmd_build() {
  mkdir -p "$stage/ctx"
  cp "$GC_BIN" "$stage/ctx/gc"; cp "$BD_BIN" "$stage/ctx/bd"
  [ "$("$stage/ctx/gc" version 2>/dev/null)" = 1.5.1 ] || die "gc in the context is not 1.5.1"
  cp "$(readlink -f "$(command -v claude)")" "$stage/ctx/claude"
  if [ ! -x "$stage/ctx/herdr" ]; then
    dl=$(mktemp -d "$stage/dl.XXXXXX")
    gh release download "v$HERDR_VER" --repo ogulcancelik/herdr --pattern herdr-linux-x86_64 --dir "$dl"
    echo "$HERDR_SHA  $dl/herdr-linux-x86_64" | sha256sum -c - || die "herdr does not match the pinned sha256"
    install -m 0755 "$dl/herdr-linux-x86_64" "$stage/ctx/herdr"; rm -f "$dl/herdr-linux-x86_64"; rmdir "$dl"
  fi
  [ "$("$stage/ctx/herdr" --version)" = "herdr $HERDR_VER" ] || die "herdr in the context is not $HERDR_VER"
  if [ ! -x "$stage/ctx/dolt" ]; then
    dl=$(mktemp -d "$stage/dl.XXXXXX")
    gh release download "v$DOLT_VER" --repo dolthub/dolt --pattern dolt-linux-amd64.tar.gz --dir "$dl"
    tar -xzf "$dl/dolt-linux-amd64.tar.gz" -C "$dl"
    install -m 0755 "$dl/dolt-linux-amd64/bin/dolt" "$stage/ctx/dolt"
    rm -f "$dl/dolt-linux-amd64.tar.gz" "$dl/dolt-linux-amd64/bin/dolt" "$dl/dolt-linux-amd64/LICENSES" "$dl/dolt-linux-amd64/bin/install.sh" 2>/dev/null || true
  fi
  cp "$here/Dockerfile" "$stage/Dockerfile"
  docker build -t "$img" "$stage"
}

cmd_up() {
  l=$(load1); awk -v l="$l" 'BEGIN{exit !(l>=8)}' && die "host load1 $l >= 8, not starting (mayor ruling 2)"
  docker volume create "$vol" >/dev/null
  # a box the kill switch stopped is still there as an exited container: drop it, the volume keeps the city
  [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo none)" = true ] && die "box $name is already running"
  docker rm -f "$name" >/dev/null 2>&1 || true
  # Own pid namespace, bounded pids/cpus/memory. No --privileged, no host mounts, no host network, no docker socket,
  # nothing of the live town's: ~/gc, ~/.gc, ~/.claude and the supervisor's env stay out of reach.
  docker run -d --init --name "$name" --cpus "$cpus" --cpu-shares 256 --memory "$mem" --pids-limit 1500 \
    -e PATH=/home/ubuntu/.local/bin:/home/ubuntu/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    -p 127.0.0.1:7317:7317 -v "$vol:/home/ubuntu" "$img" >/dev/null
  log "box $name up (cpus=$cpus mem=$mem pids<=1500)"
}

cmd_exec() { docker exec -u ubuntu -e HOME=/home/ubuntu -w /home/ubuntu "$name" "$@"; }
cmd_shell() { docker exec -it -u ubuntu -e HOME=/home/ubuntu -w /home/ubuntu "$name" bash -l; }

cmd_stats() {
  local procs zomb boxload
  procs=$(docker exec "$name" sh -c 'pgrep -c -x herdr || true' 2>/dev/null | tr -d '[:space:]')
  zomb=$(docker exec "$name" sh -c 'ps -eo stat | grep -c "^Z" || true' 2>/dev/null | tr -d '[:space:]')
  boxload=$(docker exec "$name" cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo '?')
  printf '%s herdr=%s zombies=%s box_load=%s host_load=%s\n' "$(date -u +%FT%TZ)" "${procs:-?}" "${zomb:-?}" "$boxload" "$(cut -d' ' -f1-3 /proc/loadavg)"
}

# watch TSV [--every N]: the host-side kill switch and sampler (watchdog.py has the rules)
cmd_watch() { exec python3 -I -B "$here/watchdog.py" --name "$name" "$@"; }

cmd_kill() {
  log "KILL SWITCH: killing $name"
  docker kill "$name" >/dev/null 2>&1 || true
}

cmd_down() { docker rm -f "$name" >/dev/null 2>&1 || true; log "box $name removed (volume $vol kept)"; }

case ${1:-} in
  build) cmd_build ;;
  up) cmd_up ;;
  exec) shift; cmd_exec "$@" ;;
  shell) cmd_shell ;;
  stats) cmd_stats ;;
  watch) shift; cmd_watch "$@" ;;
  kill) cmd_kill ;;
  down) cmd_down ;;
  *) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
