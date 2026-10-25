#!/usr/bin/env bash
# COLD backup of everything the 1.5.1 upgrade can change, so rollback.sh can put the town back exactly as it was.
# It refuses to run unless the supervisor and the city's managed Dolt are stopped: a copy of a running Dolt data dir is
# only crash-consistent, and the schema migration that bd 1.3.x does is one way (53 -> 66, old clients cannot read it),
# so this copy is the only way back.
#
# Writes $BACKUP_ROOT/<utc>-gc1.4.1/ and points $BACKUP_ROOT/latest at it (last, so a half-written backup is never "latest"):
#   dolt/                the city's managed Dolt data dir (every database: hq + the rig databases)
#   city-files.tar       city.toml pack.toml packs.lock and the pack dirs (formulas orders assets commands ...)
#   city-gc.tar          <city>/.gc without worktrees and the big regenerable trace/cache dirs
#   city-beads.tar       <city>/.beads without the Dolt data dir and old archives
#   rig-beads/<rig>.tar  each rig's .beads config files (rig databases live in the city Dolt)
#   home-gc.tar          ~/.gc (supervisor.toml cities.toml cache logs)
#   local.tar            ~/.local/bin/{gc,bd}, the supervisor unit and its drop-ins (paths relative to /)
#   MANIFEST.txt         versions, dolt HEADs, tar checksums, git HEAD of the city
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

step "refusing to back up a live town"
systemctl --user is-active --quiet "$UNIT_NAME" && die "$UNIT_NAME is still active: stop the city and the supervisor first (apply.sh does)"
if city_dolt_running; then die "the city's dolt sql-server is still running"; fi
if pgrep -af 'gc supervisor run' >/dev/null; then die "a gc supervisor process is still running"; fi
load_ok || die "load $(load1) > $LOAD_MAX"
log "cold: no supervisor, no city dolt"

B=$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-gc1.4.1
mkdir -p "$B/rig-beads"
log "backup dir: $B"

step "dolt data dir ($(du -sh "$CITY/.beads/dolt" | cut -f1)): cp -a"
t0=$(date +%s)
cp -a "$CITY/.beads/dolt" "$B/dolt"
log "copied in $(( $(date +%s) - t0 ))s"

tarit() {  # tarit <out.tar> <tar args...>
  local out=$1; shift
  tar -cf "$B/$out" "$@"
  log "$out $(du -h "$B/$out" | cut -f1)"
}

step "small trees"
tarit city-files.tar --ignore-failed-read -C "$CITY" \
  city.toml pack.toml packs.lock formulas orders assets commands agents overlays template-fragments quality-bar doctor AGENTS.md CLAUDE.md .claude
# GNU tar's --exclude is positional: it only applies to the paths that follow it, so every exclude comes first.
tarit city-gc.tar \
  --exclude='.gc/worktrees' --exclude='.gc/runtime/session-reconciler-trace' --exclude='.gc/runtime/product-check' \
  --exclude='.gc/runtime/ui-preview' --exclude='.gc/runtime/packs/maintenance' --exclude='.gc/agents' \
  -C "$CITY" .gc
tarit city-beads.tar \
  --exclude='.beads/dolt' --exclude='.beads/dolt.*' --exclude='.beads/embeddeddolt*' --exclude='.beads/legacy-sqlite-bak-*' \
  --exclude='.beads/interactions.jsonl' --exclude='.beads/hooks.log' \
  -C "$CITY" .beads
while read -r rig_path; do
  [ -d "$rig_path/.beads" ] || { log "skip $rig_path (no .beads)"; continue; }
  tarit "rig-beads/$(basename "$rig_path").tar" \
    --exclude='.beads/backup' --exclude='.beads/embeddeddolt' --exclude='.beads/br-shadow-*' \
    --exclude='.beads/interactions.jsonl' --exclude='.beads/*.log' \
    -C "$rig_path" .beads
done < <(sed -n 's/^path = "\(.*\)"/\1/p' "$CITY/.gc/site.toml")
tarit home-gc.tar -C "$HOME" .gc
tarit local.tar --ignore-failed-read -C / \
  "${WRAP_GC#/}" "${WRAP_BD#/}" "${UNIT_FILE#/}" "${HOME#/}/.config/systemd/user/$UNIT_NAME.service.d"

step "manifest"
{
  echo "backup of gc $("$GC_OLD" version 2>&1) / $("$BD_OLD" version 2>&1 | head -1) / $(dolt version | head -1)"
  echo "taken $(ts) on $(hostname), load $(load1)"
  echo "city $CITY  git HEAD $(git -C "$CITY" rev-parse HEAD 2>/dev/null)"
  echo "unit: $(grep '^ExecStart=' "$UNIT_FILE")"
  echo "wrapper gc: $(grep '^exec ' "$WRAP_GC")"
  echo "wrapper bd: $(grep -m1 '^exec /' "$WRAP_BD")"
  echo; echo "== dolt heads (offline, from the copy's source)"; "$UPG/dolt-heads.sh"
  echo; echo "== sha256"; (cd "$B" && sha256sum ./*.tar rig-beads/*.tar)
  echo; echo "== dolt dir: $(find "$B/dolt" -type f | wc -l) files, $(du -sb "$B/dolt" | cut -f1) bytes"
} > "$B/MANIFEST.txt"
cat "$B/MANIFEST.txt"

printf '%s\n' "$B" > "$BACKUP_ROOT/latest.new" && mv -f "$BACKUP_ROOT/latest.new" "$BACKUP_ROOT/latest"
step "backup complete: $B"
