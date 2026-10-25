#!/usr/bin/env bash
# Put the town back on gc 1.4.1 / bd 1.2.2 / the Dolt data it had before apply.sh, from the cold backup.
# Needed because bd 1.3.x migrates the Dolt schema one way (53 -> 66): a repoint to the old binaries alone cannot read it.
# Run it detached:  run-detached.sh rollback.sh [backup-dir]     (default: whatever $BACKUP_ROOT/latest names)
#
# The upgraded Dolt data dir is MOVED aside (.beads/dolt.1.5.1-<utc>), never deleted, and the backup is COPIED in, so the
# backup stays intact and a half-failed rollback can be redone. Delete the moved-aside dir by hand once the town is happy.
# Beads written between apply.sh and this rollback are in the moved-aside dir only: they are not carried back.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

B=${1:-$(backup_dir)}
[ -f "$B/MANIFEST.txt" ] && [ -d "$B/dolt" ] && [ -f "$B/home-gc.tar" ] && [ -f "$B/local.tar" ] \
  || die "$B is not a complete backup (MANIFEST.txt, dolt/, home-gc.tar, local.tar)"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log "rollback from $B (gc $(sed -n 's/^backup of gc \([^ ]*\).*/\1/p' "$B/MANIFEST.txt"))"

step "1. stop everything, on whichever gc is running"
cur_gc=$(sed -n 's/^ExecStart=\(.*\) supervisor run$/\1/p' "$UNIT_FILE" 2>/dev/null || true)
[ -x "${cur_gc:-}" ] || cur_gc=$GC_NEW
if systemctl --user is-active --quiet "$UNIT_NAME"; then
  timeout 600 "$cur_gc" stop || log "gc stop exited $? (1.5.x can exit 1: 'stop not confirmed'); verifying below"
  systemctl --user stop "$UNIT_NAME"
fi
stop_city_dolt
if tmux -L gc ls >/dev/null 2>&1; then
  log "sessions survived gc stop, killing the city's tmux server (socket -L gc)"
  tmux -L gc kill-server || true
fi
systemctl --user is-active --quiet "$UNIT_NAME" && die "$UNIT_NAME still active"
! pgrep -af 'dolt sql-server' | grep -q "$CITY/" || die "the city's dolt is still running"
! tmux -L gc ls >/dev/null 2>&1 || die "tmux -L gc still has sessions"
log "town is down"

step "2. Dolt data dir: move the upgraded one aside, copy the backup in"
if [ -e "$CITY/.beads/dolt" ]; then mv "$CITY/.beads/dolt" "$CITY/.beads/dolt.1.5.1-$stamp"; log "moved aside: $CITY/.beads/dolt.1.5.1-$stamp"; fi
t0=$(date +%s)
cp -a "$B/dolt" "$CITY/.beads/dolt"
log "restored in $(( $(date +%s) - t0 ))s"

step "3. small trees (config, packs, .gc, .beads files, rig .beads, ~/.gc, wrappers, unit)"
tar -xf "$B/city-files.tar" -C "$CITY"
tar -xf "$B/city-gc.tar" -C "$CITY"
tar -xf "$B/city-beads.tar" -C "$CITY"
while read -r rig_path; do
  t=$B/rig-beads/$(basename "$rig_path").tar
  [ -f "$t" ] && tar -xf "$t" -C "$rig_path" || log "no backup tar for $rig_path"
done < <(sed -n 's/^path = "\(.*\)"/\1/p' "$CITY/.gc/site.toml")
tar -xf "$B/home-gc.tar" -C "$HOME"
tar -xf "$B/local.tar" -C /
systemctl --user daemon-reload
grep -q "^ExecStart=$GC_OLD supervisor run\$" "$UNIT_FILE" || die "unit ExecStart is not $GC_OLD after restore: $(grep ^ExecStart= "$UNIT_FILE")"
grep -q "^exec $BREW/bin/gc " "$WRAP_GC" || die "$WRAP_GC not restored"
log "unit and wrappers are back on brew's gc 1.4.1 / bd 1.2.2"

step "4. the restored data equals the backup (offline, per database)"
"$UPG/dolt-heads.sh" > "$B/heads-after-rollback.txt"
"$UPG/dolt-heads.sh" "$B/dolt" > "$B/heads-backup.txt"
diff "$B/heads-backup.txt" "$B/heads-after-rollback.txt" || die "restored Dolt heads differ from the backup"
cat "$B/heads-after-rollback.txt"

step "5. start on 1.4.1"
systemctl --user start "$UNIT_NAME"
wait_supervisor "$GC_OLD"
start_city "$GC_OLD"
wait_city_ready "$GC_OLD"

step "6. verify"
"$GC_OLD" version
systemctl --user show "$UNIT_NAME" -p MainPID -p ExecStart --no-pager | cut -c1-200
"$UPG/postcheck.sh" --expect-old

step "rollback complete"
echo "Upgraded data kept at $CITY/.beads/dolt.1.5.1-$stamp ($(du -sh "$CITY/.beads/dolt.1.5.1-$stamp" | cut -f1)); remove it when satisfied."
