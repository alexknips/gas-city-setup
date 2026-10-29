#!/usr/bin/env bash
# Entry point of the pr-review-run order: pr_review.py at low CPU and IO priority (README.md, "Review").
#   pr-review-run.sh                          CI escalations, then the next queued PR within the cap
#   pr-review-run.sh --pr owner/repo#N        review this PR now (counts against the cap)
#   pr-review-run.sh --dry-run [--pr R#N]     pick, prepare and build the prompt; no model, no post
set -u
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gc-bin.sh"
prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
exec "${prio[@]}" python3 "$here/pr_review.py" "$@"
