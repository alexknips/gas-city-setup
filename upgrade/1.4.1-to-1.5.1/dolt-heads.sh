#!/usr/bin/env bash
# One line per database of a Dolt data dir: HEAD commit and issue count. Read with the dolt CLI straight from the
# directory, so it works with the server stopped (the state to compare before a migration and after a rollback).
#   dolt-heads.sh [data-dir]      default: the city's managed Dolt, $CITY/.beads/dolt
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
root=${1:-$CITY/.beads/dolt}
for db in $(find "$root" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' | sort); do
  d=$root/$db
  head=$(cd "$d" && dolt log -n 1 --oneline 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk 'NR==1{print $1}')
  n=$(cd "$d" && dolt sql -r csv -q 'select count(*) from issues' 2>/dev/null | tail -1)
  printf '%s head=%s issues=%s\n' "$db" "${head:-?}" "${n:-?}"
done
