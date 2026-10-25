#!/usr/bin/env bash
# Pre-flight for the gc 1.4.1 -> 1.5.1 upgrade. Read only: it changes nothing on the town.
# Exit 0 = go (warnings allowed), exit 1 = at least one blocking FAIL. Run it right before apply and again if the
# window slips.
#   precheck.sh [--strict]     --strict makes in-flight polecat work a FAIL too (default: WARN, since the apply
#                              restarts every session and a polecat resumes from its hook and its worktree)
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
strict=${1:-}
fails=0 warns=0
pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; warns=$((warns+1)); }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails+1)); }

step "host"
if load_ok; then pass "load1 $(load1) <= $LOAD_MAX"; else fail "load1 $(load1) > $LOAD_MAX (the town's stop rule)"; fi
awk -v l="$(cut -d' ' -f3 /proc/loadavg)" 'BEGIN{exit !(l>15)}' && warn "load15 $(cut -d' ' -f3 /proc/loadavg) is high: the 7 GB copy and the first start will be slow"

step "binaries"
[ -x "$GC_OLD" ] && pass "old gc: $("$GC_OLD" version 2>&1)" || fail "old gc missing: $GC_OLD (rollback target)"
[ -x "$BD_OLD" ] && pass "old bd: $("$BD_OLD" version 2>&1 | head -1)" || fail "old bd missing: $BD_OLD (rollback target)"
if [ -x "$GC_NEW" ]; then
  v=$("$GC_NEW" version 2>&1); [ "$v" = "$GC_VER" ] && pass "new gc: $v" || fail "new gc reports '$v', want $GC_VER"
else fail "new gc not installed: run install.sh"; fi
if [ -x "$BD_NEW" ]; then
  v=$("$BD_NEW" version 2>&1 | head -1); case $v in *"$BD_VER"*) pass "new bd: $v";; *) fail "new bd reports '$v', want $BD_VER";; esac
else fail "new bd not installed: run install.sh"; fi
d=$(command -v dolt) && pass "dolt (left alone): $("$d" version | head -1)" || fail "no dolt on PATH"

step "wrappers and unit are the shape apply.sh expects"
grep -q "^exec $BREW/bin/gc " "$WRAP_GC" && pass "$WRAP_GC execs $BREW/bin/gc" || fail "$WRAP_GC is not the expected passthrough (apply.sh would not repoint it)"
grep -q "^exec $BREW/bin/bd " "$WRAP_BD" && pass "$WRAP_BD execs $BREW/bin/bd" || fail "$WRAP_BD is not the expected passthrough"
grep -q "^ExecStart=$GC_OLD supervisor run\$" "$UNIT_FILE" && pass "unit ExecStart is $GC_OLD" || fail "unit ExecStart differs from $GC_OLD: $(grep ^ExecStart= "$UNIT_FILE")"
systemctl --user is-active --quiet "$UNIT_NAME" && pass "supervisor unit active" || warn "supervisor unit is not active (a cold start; fine if intended)"
systemctl --user list-units --state=active --plain --no-legend 'gcu-apply-*' 2>/dev/null | grep -q . && fail "an apply unit (gcu-apply-*) is already running"

step "city health"
st=$(mktemp "${TMPDIR:-/tmp}/gcu-precheck-status.XXXXXX")
if timeout 180 "$GC_OLD" status >"$st" 2>&1; then pass "gc status ok"; else warn "gc status failed or timed out (see below)"; head -5 "$st"; fi
grep -q 'Controller: *supervisor-managed' "$st" && pass "controller is supervisor-managed" || warn "controller line not 'supervisor-managed'"
rm -f "$st"

step "work in flight (every scope: the city and each rig)"
mapfile -t scopes < <({ echo "$CITY"; sed -n 's/^path = "\(.*\)"/\1/p' "$CITY/.gc/site.toml"; } | sort -u)
inflight=0
for s in "${scopes[@]}"; do
  [ -d "$s/.beads" ] || { warn "no .beads in $s"; continue; }
  # two steps, not a pipe: a failing bd must not look like "nothing in flight"
  json=$(cd "$s" && timeout 120 "$BD_OLD" list --status in_progress --json --limit 0 2>/dev/null) \
    && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$json" || { fail "could not list in_progress beads in $s (bd failed or printed no JSON array)"; continue; }
  rows=$(jq -r '.[] | [.id, (.assignee // "-"), (.title // "")[0:60]] | @tsv' <<<"$json")
  [ -n "$rows" ] || continue
  while IFS=$'\t' read -r id who title; do
    inflight=$((inflight+1))
    case $who in
      *refinery*) fail "refinery has $id in_progress (mid-merge): $title  [$s]" ;;
      *) if [ "$strict" = --strict ]; then fail "in flight: $id $who: $title"; else warn "in flight: $id $who: $title"; fi ;;
    esac
  done <<<"$rows"
done
[ "$inflight" -gt 0 ] || pass "no in_progress beads in any scope"

step "disk"
dolt_kb=$(du -sk "$CITY/.beads/dolt" | cut -f1)
need_kb=$((dolt_kb * 5 / 2))
mkdir -p "$BACKUP_ROOT" 2>/dev/null
free_kb=$(df -Pk "$BACKUP_ROOT" | awk 'NR==2{print $4}')
if [ "$free_kb" -ge "$need_kb" ]; then pass "free $((free_kb/1048576)) GiB >= 2.5 x dolt $((dolt_kb/1048576)) GiB (backup + a restore copy for rollback)"
else fail "free $((free_kb/1048576)) GiB < $((need_kb/1048576)) GiB needed (2.5 x dolt $((dolt_kb/1048576)) GiB) at $BACKUP_ROOT"; fi

step "network (only needed if doctor or a pack fetch has to reach GitHub)"
curl -fsS -m 10 -o /dev/null -I https://github.com && pass "github reachable" || warn "github not reachable: gc 1.5.1 serves the core/bd packs from the binary, but a missing pack cache would block start"
# cache dirs are named by hash: match on each clone's origin URL instead
origins=$(for d in "$HOME"/.gc/cache/repos/*/; do git -C "$d" remote get-url origin 2>/dev/null; done)
for p in gastownhall/gascity-packs.git alexknips/gas-city-setup.git; do
  grep -q "$p" <<<"$origins" && pass "pack cache has a clone of $p" || warn "no cached clone of $p in ~/.gc/cache/repos (doctor --fix / start would have to fetch it)"
done

step "city repo state (informational; backup.sh copies the files, not git)"
if git -C "$CITY" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$CITY" status --short -- city.toml pack.toml packs.lock formulas orders assets commands agents | sed 's/^/      /'
  git -C "$CITY" rev-parse --short HEAD | sed 's/^/      HEAD /'
else echo "      $CITY is not a git repo (expected only in the rehearsal box)"; fi

printf '\n%s fails=%s warns=%s\n' "$(ts)" "$fails" "$warns"
[ "$fails" -eq 0 ] && echo "PRECHECK OK" || { echo "PRECHECK FAILED"; exit 1; }
