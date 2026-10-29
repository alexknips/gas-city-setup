#!/usr/bin/env bash
# Entry point of the pr-review-feedback order: pr_feedback.py at low CPU and IO priority.
#   pr-review-feedback.sh                       one pass (what the order runs)
#   pr-review-feedback.sh --dry-run             counts only; writes nothing
#   pr-review-feedback.sh --record KEY VERDICT  a verdict seen by hand
set -u
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gc-bin.sh"
prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
exec "${prio[@]}" python3 "$here/pr_feedback.py" "$@"
