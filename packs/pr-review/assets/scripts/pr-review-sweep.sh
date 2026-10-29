#!/usr/bin/env bash
# Entry point of the pr-review-sweep order: pr_sweep.py at low CPU and IO priority (README.md, "Sweep").
#   pr-review-sweep.sh                one sweep (what the order runs)
#   pr-review-sweep.sh --dry-run      print what it would record; write nothing
#   pr-review-sweep.sh --backtest 7   calibration over a week of PRs
set -u
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gc-bin.sh"
prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
exec "${prio[@]}" python3 "$here/pr_sweep.py" "$@"
