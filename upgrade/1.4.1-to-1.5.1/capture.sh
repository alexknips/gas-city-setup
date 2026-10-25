#!/usr/bin/env bash
# Snapshot of everything the 1.5.1 upgrade could change, written to one directory for a before/after diff:
# versions, resolved config, doctor, orders, sessions, tmux, unit and wrapper files, per-database Dolt HEADs.
# Read only against the city. Slow on purpose-free hosts too: every gc call costs seconds, doctor several minutes.
#   capture.sh <out-dir> [--quick]      --quick skips `gc doctor`
set -uo pipefail
out=${1:?usage: capture.sh <out-dir> [--quick]}
quick=${2:-}
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
mkdir -p "$out"
cd "$CITY" || exit 1

cap() {  # cap <name> <cmd...>: output plus exit code, never fails the capture
  local name=$1; shift
  { timeout "${CAP_TIMEOUT:-900}" "$@"; echo "[exit $?]"; } > "$out/$name.txt" 2>&1
}

cap versions bash -c 'echo "gc: $(gc version 2>&1)"; echo "bd: $(bd version 2>&1 | head -1)"; echo "dolt: $(dolt version 2>&1 | head -1)"; echo "tmux: $(tmux -V)"; echo "wrapper gc: $(grep ^exec ~/.local/bin/gc)"; echo "wrapper bd: $(grep "^exec /" ~/.local/bin/bd | head -1)"'
cap config-show gc config show
cap order-list gc order list
for o in $(gc order list 2>/dev/null | awk 'NR>1 && $1 != "" {print $1}' | sort -u); do
  cap "order-show-$o" gc order show "$o"
done
cap imports gc import list
cap status gc status
cap sessions gc session list
cap rigs gc rig list
cap tmux bash -c 'tmux -L gc ls 2>&1; tmux -L gc list-panes -a -F "#{session_name} pid=#{pane_pid} #{pane_current_command}" 2>&1 | sort'
cap unit bash -c 'systemctl --user cat gascity-supervisor 2>&1; systemctl --user show gascity-supervisor -p ExecStart -p MainPID -p ActiveState 2>&1'
cap pack-files bash -c 'cd '"$CITY"' && sha256sum city.toml pack.toml packs.lock orders/*.toml formulas/*.toml assets/scripts/orphan-sweep-live-guard.sh 2>&1; echo; cat pack.toml; echo; cat packs.lock'
cap dolt-heads "$UPG/dolt-heads.sh"
[ "$quick" = --quick ] || cap doctor gc doctor
cap doctor-json gc doctor --json
echo "captured to $out"
