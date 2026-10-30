#!/usr/bin/env bash
# The regression set (README.md, "Quality loop"): every fixture through the gate. Run it before any
# style-guide, gate, rule or prompt change moves a city's pin; it must end "N of N fixtures as expected".
#   pr-review-regress.sh            the pack's synthetic fixtures + the host-local cases
# Host-local cases (real reviews; they hold the repositories' text) live in
# ${PR_REVIEW_WORK_DIR:-/var/tmp/gc-pr-review}/regression; the inbox/ under it is not run until triaged.
set -u
here=$(cd "$(dirname "$0")" && pwd)
python3 "$here/pr_format.py" --regress "$here/../../tests/fixtures" "${PR_REVIEW_WORK_DIR:-/var/tmp/gc-pr-review}/regression"
rc=$?
# The restamp's shapes and its PR list (synthetic; decision-ed7320b7); the sweep's bead closing.
python3 "$here/pr_restamp.py" --selftest > /dev/null || { echo 'pr_restamp selftest: FAIL'; rc=1; }
python3 "$here/pr_sweep.py" --selftest > /dev/null || { echo 'pr_sweep selftest: FAIL'; rc=1; }
python3 "$here/pr_client_slack.py" --selftest > /dev/null || { echo 'pr_client_slack selftest: FAIL'; rc=1; }
python3 "$here/pr_review.py" --selftest > /dev/null || { echo 'pr_review selftest: FAIL'; rc=1; }
python3 "$here/pr_fallback.py" --selftest > /dev/null || { echo 'pr_fallback selftest: FAIL'; rc=1; }
# Host-local: no public file names a town person or repository (skipped without the town overrides).
python3 "$here/pr_hygiene.py" "$here/../.." || rc=1
exit $rc
