#!/usr/bin/env bash
# One line per database of the managed Dolt: HEAD commit and issue count. Read from the data dir with the dolt CLI,
# so it works with the server stopped (the state to compare before a migration and after a rollback).
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
for db in $(dolt_dbs); do
  d=$CITY/.beads/dolt/$db
  head=$(cd "$d" && dolt log -n 1 --oneline 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk 'NR==1{print $1}')
  n=$(cd "$d" && dolt sql -r csv -q 'select count(*) from issues' 2>/dev/null | tail -1)
  printf '%s head=%s issues=%s\n' "$db" "${head:-?}" "${n:-?}"
done
