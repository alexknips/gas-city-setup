#!/usr/bin/env bash
# Clean-container proof for setup.sh: a fresh box, the one-liner as root, a scratch rig, then a second run that must be a no-op.
#   GCS_TEST_IMAGE=<24.04 base image> [GCS_BRANCH=main] [MODE=oneliner|local] test/proof.sh
# oneliner (default) curls setup.sh from this repo's origin on GitHub, so the branch must be pushed;
# local runs the working tree (mounted read-only) instead. Needs docker and a host that can run systemd in a container.
set -euo pipefail
: "${GCS_TEST_IMAGE:?set GCS_TEST_IMAGE to a 24.04 base image}"
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
branch=${GCS_BRANCH:-$(git -C "$here" branch --show-current)}
c=${CONTAINER:-gcs-proof}
raw=$(git -C "$here" remote get-url origin | sed -E 's#^git@github.com:#https://github.com/#; s#\.git$##; s#^https://github.com/#https://raw.githubusercontent.com/#')

docker build -q --build-arg BASE="$GCS_TEST_IMAGE" -t gcs-test "$here/test" >/dev/null
docker rm -f "$c" >/dev/null 2>&1 || true
docker run -d --name "$c" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock -v "$here:/src:ro" gcs-test >/dev/null
docker exec "$c" systemctl is-system-running --wait >/dev/null 2>&1 || true

if [ "${MODE:-oneliner}" = local ]; then run='bash /src/setup.sh'
else run="curl -fsSL $raw/$branch/setup.sh | GCS_BRANCH=$branch bash"; fi

echo "### run 1 (as root, fresh box): $run"
docker exec "$c" bash -c "$run"
u=$(docker exec "$c" bash -c 'ls /home | head -1')
as_user() { docker exec -u "$u" -e "XDG_RUNTIME_DIR=/run/user/$(docker exec "$c" id -u "$u")" -w "/home/$u" "$c" bash -lc "$*"; }

echo "### checks"
as_user 'gc status 2>&1 | head -8'
as_user 'command -v gc bd dolt dcg caam claude gh gcs'
as_user 'jq -e "[.hooks.PreToolUse[].hooks[].command | select(test(\"dcg\$\"))] | length > 0" ~/.claude/settings.json' >/dev/null && echo "dcg hook present in ~/.claude/settings.json"
as_user 'systemctl --user list-units --no-legend "gascity-*" "gcs-*"'

echo "### gcs rig-add (scratch repo)"
as_user 'mkdir -p ~/projects/demo && cd ~/projects/demo && git init -q -b main && git commit -q --allow-empty -m init'
as_user 'gcs rig-add ~/projects/demo'
# shellcheck disable=SC2016  # $USER must expand inside the container
as_user 'grep -A6 "name = \"demo\"" ~/gc/city.toml | head -12; jq ".projects[\"/home/$USER/projects/demo\"]" ~/.claude.json'

echo "### run 2 (must be a no-op)"
sum=$(as_user 'cksum ~/gc/city.toml ~/gc/pack.toml ~/.config/gcs/config.env ~/gc/formulas/*')
out=$(as_user 'gcs setup' 2>&1) || { echo "$out"; echo "FAIL: second run failed"; exit 1; }
echo "$out" | sed 's/\x1b\[[0-9;]*m//g' | grep -E '^(==> (installing|apt:|creating|merge mode|swap)|warn)' && { echo "FAIL: second run changed things"; exit 1; }
[ "$sum" = "$(as_user 'cksum ~/gc/city.toml ~/gc/pack.toml ~/.config/gcs/config.env ~/gc/formulas/*')" ] || { echo "FAIL: files changed on re-run"; exit 1; }
echo "second run: clean no-op"
echo "container $c left running: docker exec -it -u $u $c bash -l   (docker rm -f $c to clean up)"
