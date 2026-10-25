#!/usr/bin/env bash
# Rehearsal harness for the gc 1.4.1 -> 1.5.1 upgrade: a throwaway systemd container holding a COPY of this
# town's city config and beads data, with the host's linuxbrew tool layout. The live supervisor, ~/.gc, the Dolt
# servers and city.toml are only ever READ (rsync / cp -a from the host); nothing here writes to them.
#
#   GCU_SCRATCH=<dir with ~35 GB free> rehearse.sh <command>
#     fetch    download bd 1.2.2 + dolt 2.3.2 (the live versions) and copy the live gc 1.4.1 binary
#     snapshot copy the live Dolt data dir to $GCU_SCRATCH/stage/dolt (niced; a crash-consistent copy of a running server)
#     build    docker build the rehearsal image
#     stage    fill the docker volumes gcu-home / gcu-data from the live city (needs snapshot)
#     up       start the container and the live-identical supervisor unit on gc 1.4.1
#     exec     run a command in the box as the town user:  rehearse.sh exec 'gc status'
#     down     remove the container (volumes stay, so `up` resumes the same state)
#     purge    remove the container and both volumes
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
scratch=${GCU_SCRATCH:?set GCU_SCRATCH to a scratch dir with room for the copies}
live_city=${GCU_LIVE_CITY:-/home/ubuntu/gc}
live_home=${GCU_LIVE_HOME:-/home/ubuntu}
live_brew=${GCU_LIVE_BREW:-/home/linuxbrew/.linuxbrew}
c=${CONTAINER:-gcu-rehearsal}
img=gcu-rehearsal
vol_home=gcu-home
vol_data=gcu-data
dl=$scratch/dl ctx=$scratch/ctx small=$scratch/small stage_dolt=$scratch/stage/dolt
box_path=/home/ubuntu/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin

say() { printf '==> %s\n' "$*"; }
die() { printf 'rehearse: %s\n' "$*" >&2; exit 1; }

load_ok() {  # the town's own rule: stop above load 20
  local l; l=$(cut -d' ' -f1 /proc/loadavg)
  awk -v l="$l" 'BEGIN{exit !(l>20)}' && die "host load $l > 20, not starting"
  return 0
}

fetch() {
  mkdir -p "$dl/bd122" "$dl/dolt232" "$ctx/tools/Cellar/gascity/1.4.1/bin" "$ctx/tools/Cellar/beads/1.2.2/bin" "$ctx/tools/Cellar/dolt/2.3.2/bin" "$ctx/tools/bin"
  [ -x "$dl/bd122/bd" ] || {
    say "bd 1.2.2 (the live version)"
    curl -fsSL -o "$dl/bd122/b.tgz" https://github.com/gastownhall/beads/releases/download/v1.2.2/beads_1.2.2_linux_amd64.tar.gz
    curl -fsSL -o "$dl/bd122/sums" https://github.com/gastownhall/beads/releases/download/v1.2.2/checksums.txt
    (cd "$dl/bd122" && sha256sum --ignore-missing -c sums && tar -xzf b.tgz bd)
  }
  [ -x "$dl/dolt232/dolt" ] || {
    say "dolt 2.3.2 (the live version)"
    curl -fsSL -o "$dl/dolt232/d.tgz" https://github.com/dolthub/dolt/releases/download/v2.3.2/dolt-linux-amd64.tar.gz
    tar -xzf "$dl/dolt232/d.tgz" -C "$dl/dolt232" --strip-components=2 dolt-linux-amd64/bin/dolt
  }
  say "gc 1.4.1 (copied from the live Cellar, read only)"
  cp "$live_brew/Cellar/gascity/1.4.1/bin/gc" "$ctx/tools/Cellar/gascity/1.4.1/bin/gc"
  cp "$dl/bd122/bd" "$ctx/tools/Cellar/beads/1.2.2/bin/bd"
  cp "$dl/dolt232/dolt" "$ctx/tools/Cellar/dolt/2.3.2/bin/dolt"
  ln -sfn ../Cellar/gascity/1.4.1/bin/gc "$ctx/tools/bin/gc"
  ln -sfn ../Cellar/beads/1.2.2/bin/bd "$ctx/tools/bin/bd"
  ln -sfn ../Cellar/dolt/2.3.2/bin/dolt "$ctx/tools/bin/dolt"
}

snapshot() {
  load_ok
  mkdir -p "$scratch/stage"
  [ ! -e "$stage_dolt" ] || die "$stage_dolt exists; use a fresh GCU_SCRATCH to re-snapshot"
  say "copying $live_city/.beads/dolt (niced, read only on the live side)"
  /usr/bin/time -v nice -n 19 ionice -c3 cp -a "$live_city/.beads/dolt" "$stage_dolt" 2> "$scratch/stage/snapshot-time.log"
  grep -E 'Elapsed|Maximum resident' "$scratch/stage/snapshot-time.log" || true
}

build() {
  cp "$here/Dockerfile" "$here/fake-claude" "$ctx/"
  docker build -q -t "$img" "$ctx"
}

# The rig repos only need to exist with their .beads config: the rehearsal never builds or merges anything.
stage_small() {
  [ -d "$small" ] && die "$small exists; use a fresh GCU_SCRATCH"
  mkdir -p "$small/home/gc" "$small/home/.gc" "$small/home/.local/bin" "$small/home/.local/share/systemd/user" \
           "$small/home/.config/systemd/user" "$small/data/projects"
  say "city files"
  rsync -a --exclude=/.git --exclude=/.gc --exclude=/.beads --exclude='/.dolt-*' --exclude='/hu-*' \
        --exclude='/.trash-*' --exclude=/.ruff_cache --exclude=/.runtime --exclude=/.claude --exclude=/.codex \
        --exclude=/.doctor --exclude=/worktrees --exclude='*.bak*' --exclude=__pycache__ \
        "$live_city/" "$small/home/gc/"
  mkdir -p "$small/home/gc/.gc" "$small/home/gc/.beads"
  cp "$live_city/.gc/site.toml" "$live_city/.gc/settings.json" "$small/home/gc/.gc/"
  rsync -a --exclude=/dolt --exclude='/dolt.*' --exclude='/embeddeddolt*' --exclude=/hooks.log \
        --exclude=/interactions.jsonl --exclude='/.br_*' --exclude='/legacy-sqlite-bak-*' --exclude='/*.bak*' \
        --exclude='/*.dolt-migration-bak*' --exclude=/dolt-server.port --exclude=/last-touched \
        "$live_city/.beads/" "$small/home/gc/.beads/"
  say "user-level gc state, wrappers, supervisor unit"
  cp "$live_home/.gc/cities.toml" "$live_home/.gc/supervisor.toml" "$small/home/.gc/"
  rsync -a "$live_home/.gc/cache/" "$small/home/.gc/cache/"
  cp "$live_home/.local/bin/gc" "$live_home/.local/bin/bd" "$small/home/.local/bin/"
  cp "$live_home/.local/share/systemd/user/gascity-supervisor.service" "$small/home/.local/share/systemd/user/"
  mkdir -p "$small/home/.config/systemd/user/gascity-supervisor.service.d"
  cp "$live_home/.config/systemd/user/gascity-supervisor.service.d/"*.conf "$small/home/.config/systemd/user/gascity-supervisor.service.d/"
  say "rig repos (stubs with the live .beads config)"
  python3 - "$live_city" "$small/data" <<'PY'
import subprocess, sys, tomllib
city, out = sys.argv[1], sys.argv[2]
site = tomllib.load(open(f"{city}/.gc/site.toml", "rb"))
conf = tomllib.load(open(f"{city}/city.toml", "rb"))
branch = {r["name"]: r.get("default_branch", "main") for r in conf["rigs"]}
for r in site["rig"]:
    src, dst = r["path"], out + "/projects/" + r["path"].rsplit("/", 1)[1]
    subprocess.run(["mkdir", "-p", dst + "/.beads"], check=True)
    subprocess.run(["rsync", "-a", "--exclude=/dolt", "--exclude=/embeddeddolt", "--exclude=/backup",
                    "--exclude=/br-shadow-*", "--exclude=*.log", "--exclude=/interactions.jsonl",
                    "--exclude=/issues.jsonl*", "--exclude=/.write.lock*", "--exclude=/dolt-server.port",
                    "--exclude=/last-touched", "--exclude=/hooks", src + "/.beads/", dst + "/.beads/"], check=True)
    g = ["git", "-C", dst, "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", branch.get(r["name"], "main"), dst], check=True)
    url = subprocess.run(["git", "-C", src, "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    if url:
        subprocess.run(g + ["remote", "add", "origin", url], check=True)
    subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "rehearsal stub"], check=True)
PY
}

stage() {
  load_ok
  [ -d "$stage_dolt" ] || die "no $stage_dolt: run snapshot first"
  docker image inspect "$img" >/dev/null 2>&1 || die "no image $img: run fetch, build first"
  docker volume create "$vol_home" >/dev/null; docker volume create "$vol_data" >/dev/null
  stage_small
  say "loading the small trees into the volumes"
  tar -C "$small/home" -cf - . | docker run --rm -i -v "$vol_home:/h" --entrypoint tar "$img" -xf - -C /h
  docker run --rm -v "$vol_data:/d" --entrypoint sh "$img" -c 'chown 1000:1000 /d'
  tar -C "$small/data" -cf - . | docker run --rm -i -v "$vol_data:/d" --entrypoint tar "$img" -xf - -C /d
  say "loading the Dolt snapshot into the volume (cp -a)"
  docker run --rm -v "$vol_home:/h" -v "$stage_dolt:/s:ro" --entrypoint sh "$img" -c \
    'mkdir -p /h/gc/.beads && cp -a /s /h/gc/.beads/dolt && chown 1000:1000 /h /h/gc /h/gc/.beads'
}

up() {
  load_ok
  docker rm -f "$c" >/dev/null 2>&1 || true
  # --cpus / --cpu-shares keep the box a polite neighbour of the live town
  docker run -d --name "$c" --privileged --cpus "${GCU_CPUS:-6}" --cpu-shares 256 --memory "${GCU_MEM:-16g}" \
    --tmpfs /run --tmpfs /run/lock -v "$vol_home:/home/ubuntu" -v "$vol_data:/data" -v "$here/..:/upgrade:ro" "$img" >/dev/null
  docker exec "$c" systemctl is-system-running --wait >/dev/null 2>&1 || true
  local i  # logind may not be ready on the first try: retry linger and start the user manager by hand
  for i in $(seq 30); do
    docker exec "$c" test -S /run/user/1000/bus && break
    docker exec "$c" loginctl enable-linger ubuntu >/dev/null 2>&1 && docker exec "$c" systemctl start user@1000.service >/dev/null 2>&1
    sleep 2
  done
  docker exec "$c" test -S /run/user/1000/bus || die "no user bus in the box"
  # the live host has a global dolt identity (gc refuses to start the bead store without one); the box gets a throwaway
  x 'dolt config --global --add user.name "gcu rehearsal" && dolt config --global --add user.email rehearsal@example.invalid && dolt config --global --add versioncheck.disabled true' >/dev/null
  x 'systemctl --user daemon-reload && systemctl --user enable --now gascity-supervisor && systemctl --user is-active gascity-supervisor'
}

x() {  # run a command in the box as the town user, with the live shell's PATH shape
  docker exec -u ubuntu -w "${XW:-/home/ubuntu/gc}" -e HOME=/home/ubuntu -e XDG_RUNTIME_DIR=/run/user/1000 \
    -e DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$c" bash -c "export PATH=$box_path; $*"
}

case "${1:-}" in
  fetch) fetch ;;
  snapshot) snapshot ;;
  build) build ;;
  stage) stage ;;
  up) up ;;
  exec) shift; x "$*" ;;
  down) docker rm -f "$c" ;;
  purge) docker rm -f "$c" 2>/dev/null || true; docker volume rm "$vol_home" "$vol_data" ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
