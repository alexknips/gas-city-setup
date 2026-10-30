#!/usr/bin/env bash
# Entry point of the pr-review-client-queue order: pr_client_slack.py --queue at low CPU and IO priority
# (README.md, "The client's Slack").
#   pr-review-client-queue.sh          the queue post when it is due (what the order runs)
#   pr-review-client-queue.sh --now    whatever the time (with PR_REVIEW_CLIENT_SLACK=dry-run: print only)
set -u
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gc-bin.sh"
prio=(nice -n 10)
command -v ionice >/dev/null 2>&1 && prio+=(ionice -c 3)
exec "${prio[@]}" python3 "$here/pr_client_slack.py" --queue "$@"
