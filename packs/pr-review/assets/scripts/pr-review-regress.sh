#!/usr/bin/env bash
# The regression set (README.md, "Quality loop"): every fixture through the gate. Run it before any
# style-guide, gate, rule or prompt change moves a city's pin; it must end "N of N fixtures as expected".
#   pr-review-regress.sh            the pack's synthetic fixtures + the host-local cases
# Host-local cases (real reviews; they hold the repositories' text) live in
# ${PR_REVIEW_WORK_DIR:-/var/tmp/gc-pr-review}/regression; the inbox/ under it is not run until triaged.
set -u
here=$(cd "$(dirname "$0")" && pwd)
exec python3 "$here/pr_format.py" --regress "$here/../../tests/fixtures" "${PR_REVIEW_WORK_DIR:-/var/tmp/gc-pr-review}/regression"
