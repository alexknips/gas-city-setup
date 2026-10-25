#!/usr/bin/env bash
# Post-flight for the gc 1.4.1 -> 1.5.1 upgrade (and, with --expect-old, for the rollback). Read only.
# Exit 0 = every blocking check passed (warnings are listed), exit 1 = something the town needs is wrong.
#   postcheck.sh [--expect-old] [--no-doctor]
#     --expect-old   the town should be back on gc 1.4.1 / bd 1.2.2 (use after rollback.sh)
#     --no-doctor    skip `gc doctor` (it takes minutes: 6m44s measured on a loaded host)
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
want=new doctor=1
for a in "$@"; do case $a in --expect-old) want=old ;; --no-doctor) doctor=0 ;; *) die "unknown arg $a" ;; esac; done
if [ "$want" = new ]; then WANT_GC=$GC_NEW WANT_GCV=$GC_VER WANT_BDV=$BD_VER; else WANT_GC=$GC_OLD WANT_GCV=1.4.1 WANT_BDV=1.2.2; fi
fails=0 warns=0
pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; warns=$((warns+1)); }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails+1)); }
cd "$CITY" || exit 1

step "which gc and bd the town runs ($want)"
pid=$(systemctl --user show "$UNIT_NAME" -p MainPID --value)
if [ "${pid:-0}" -gt 0 ] && [ "$(readlink "/proc/$pid/exe")" = "$(readlink -f "$WANT_GC")" ]; then pass "supervisor pid $pid runs $WANT_GC"
else fail "supervisor pid '${pid:-none}' runs '$(readlink "/proc/${pid:-0}/exe" 2>/dev/null)', want $WANT_GC"; fi
v=$(gc version 2>&1); [ "$v" = "$WANT_GCV" ] && pass "gc on PATH (wrapper): $v" || fail "gc on PATH reports '$v', want $WANT_GCV"
v=$(bd version 2>&1 | head -1); case $v in *"$WANT_BDV"*) pass "bd on PATH (wrapper): $v";; *) fail "bd on PATH reports '$v', want $WANT_BDV";; esac
dolt_pid=$(cat "$CITY/.gc/runtime/packs/dolt/dolt.pid" 2>/dev/null)
if [ -n "$dolt_pid" ] && kill -0 "$dolt_pid" 2>/dev/null; then pass "city dolt up (pid $dolt_pid, $(dolt version | head -1), kept as is)"; else fail "city dolt is not running"; fi

step "city is up"
st=$(timeout 300 gc status 2>&1)
grep -q 'Controller: *supervisor-managed' <<<"$st" && pass "controller supervisor-managed" || fail "controller is not supervisor-managed"
grep -q 'partial status' <<<"$st" && warn "gc status says 'partial status' (probe timeout: the city is still starting, or Dolt is slow)"
n=$(tmux -L gc ls 2>/dev/null | wc -l)
[ "$n" -gt 0 ] && pass "$n tmux sessions on socket gc" || fail "no tmux sessions on socket gc"
for s in mayor deacon; do tmux -L gc ls 2>/dev/null | grep -q "$s" && pass "session $s is up" || warn "no tmux session matching '$s' yet"; done

step "config and the town's overrides still load"
cfg=$(mktemp "${TMPDIR:-/tmp}/gcu-config.XXXXXX")
if gc config show >"$cfg" 2>&1; then pass "gc config show ($(wc -l <"$cfg") lines)"; else fail "gc config show failed: $(head -3 "$cfg")"; fi
for k in beads-health gate-sweep order-tracking-sweep dolt-health cross-rig-deps nudge-mail-sweep spawn-storm-detect orphan-sweep; do
  grep -q "$k" "$cfg" && pass "orders.overrides $k resolved" || warn "override name $k not in gc config show (renamed or dropped?)"
done
rm -f "$cfg"
ol=$(mktemp "${TMPDIR:-/tmp}/gcu-orders.XXXXXX")
gc order list >"$ol" 2>&1
for o in orphan-sweep mayor-mail-wake alex-daily deacon-patrol-wake hivemind-deploy-nightly quality-metrics; do
  grep -q "^$o " "$ol" && pass "order $o listed" || fail "order $o missing from gc order list"
done
rm -f "$ol"
for o in mayor-mail-wake deacon-patrol-wake alex-daily; do
  s=$CITY/orders/$o.sh; [ -x "$s" ] || s=$(command ls "$CITY"/assets/scripts/$o.sh 2>/dev/null)
  if [ -n "$s" ] && [ -x "$s" ]; then
    out=$(timeout 120 "$s" --dry-run 2>&1) && pass "$o --dry-run ok" || warn "$o --dry-run exit $?: $(tail -1 <<<"$out")"
  else warn "no executable script for $o to dry-run"; fi
done

step "beads are readable in every scope (the schema moved 53 -> 66 on the way up)"
mapfile -t scopes < <({ echo "$CITY"; sed -n 's/^path = "\(.*\)"/\1/p' "$CITY/.gc/site.toml"; } | sort -u)
for s in "${scopes[@]}"; do
  c=$(cd "$s" && timeout 120 bd list --json --limit 0 2>/dev/null | jq 'length' 2>/dev/null)
  [ -n "$c" ] && pass "bd list in $s: $c open-ish beads" || fail "bd list failed in $s"
done

step "supervisor log"
lg=$GCHOME/supervisor.log
if [ "$want" = new ]; then
  c=$(grep -c 'recomputed is_blocked' "$lg" 2>/dev/null); pass "first-start bd recompute-blocked lines in supervisor.log: ${c:-0} (expect one per scope)"
fi
e=$(tail -n 400 "$lg" | grep -ci -E 'panic|fatal|cannot|refus' ); [ "${e:-0}" -eq 0 ] && pass "no panic/fatal/refused in the last 400 log lines" || warn "$e suspicious lines in the last 400 of supervisor.log: grep -i -E 'panic|fatal|cannot|refus' $lg"

if [ "$doctor" = 1 ]; then
  step "gc doctor (slow)"
  dj=$(mktemp "${TMPDIR:-/tmp}/gcu-doctor.XXXXXX")
  timeout 1500 gc doctor --json >"$dj" 2>&1; rc=$?
  if jq -e 'has("results")' "$dj" >/dev/null 2>&1; then
    jq -r '"      passed=\(.passed) warned=\(.warned) failed=\(.failed) blocking_failed=\(.blocking_failed) ok=\(.ok)"' "$dj"
    jq -r '.results[] | select(.status != "ok") | "      \(.status | ascii_upcase) [\(.severity)] \(.name): \(.message)"' "$dj"
    [ "$(jq -r '.blocking_failed' "$dj")" = 0 ] && pass "doctor: no blocking failures" || fail "doctor: blocking failures (listed above)"
    nb=$(jq '[.results[] | select(.status == "error" or .status == "failed")] | length' "$dj")
    [ "$nb" = 0 ] || warn "doctor: $nb non-blocking failed checks (listed above; compare with the baseline in the runbook)"
  else fail "gc doctor --json did not print JSON (exit $rc): $(head -c 300 "$dj")"; fi
  cp -f "$dj" "${GCU_DOCTOR_OUT:-/tmp/gcu-doctor-last.json}"; rm -f "$dj"
fi

printf '\n%s fails=%s warns=%s\n' "$(ts)" "$fails" "$warns"
[ "$fails" -eq 0 ] && echo "POSTCHECK OK" || { echo "POSTCHECK FAILED"; exit 1; }
