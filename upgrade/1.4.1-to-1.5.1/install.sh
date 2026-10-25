#!/usr/bin/env bash
# Step 1 of the upgrade, safe to run any time before the window: download the gc 1.5.1 and bd 1.3.2 release
# tarballs, verify them, and unpack them into versioned dirs under ~/.local/opt. Nothing is switched: the wrappers,
# the supervisor unit and brew's gc 1.4.1 / bd 1.2.2 stay as they are, so this is harmless to run on the live town.
# Idempotent: a version that is already installed and runs is left alone.
#
# Why not `brew upgrade`: brew would also move dolt (a dependency, 2.3.2 -> 2.6.0), delete the 1.4.1 Cellar dir the
# unit's ExecStart names, and leave no old binary to roll back to. A repoint is the whole rollback this way.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Pinned: the sha256 of the linux_amd64 tarballs as published in each release's checksums file (read 2026-10-25).
GC_TGZ=gascity_${GC_VER}_linux_amd64.tar.gz
GC_SHA=3a405a5455eb6216f7f65d4516a614e4913d83bc8964df8800e0533ab2d40044
GC_URL=https://github.com/gastownhall/gascity/releases/download/v$GC_VER
BD_TGZ=beads_${BD_VER}_linux_amd64.tar.gz
BD_SHA=c6ea3281f6ff82628351b2d0967c9eb85862c8a0ec0ebc24e63e645c8b02462e
BD_URL=https://github.com/gastownhall/beads/releases/download/v$BD_VER

[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || die "this installer pins linux_amd64 tarballs; got $(uname -s)-$(uname -m)"
work=$(mktemp -d "${TMPDIR:-/tmp}/gcu-install.XXXXXX")
trap 'rm -f "$work"/*.tar.gz "$work/gc" "$work/bd"; rmdir "$work" 2>/dev/null || true' EXIT

# fetch_unpack <name> <tgz> <sha> <url> <member> <dest-bin> <release-checksums-file>
fetch_unpack() {
  local name=$1 tgz=$2 sha=$3 url=$4 member=$5 dest=$6 sumsfile=$7
  if [ -x "$dest" ] && "$dest" version >/dev/null 2>&1; then log "$name already installed: $dest"; return; fi
  step "$name: download $tgz"
  curl -fsSL --retry 3 -o "$work/$tgz" "$url/$tgz"
  echo "$sha  $work/$tgz" | sha256sum -c - || die "$name: $tgz does not match the pinned sha256"
  # second check against the release's own checksums file, so a changed re-upload shows up as a mismatch to review
  local sums; sums=$(curl -fsSL --retry 3 "$url/$sumsfile")
  printf '%s\n' "$sums" | awk -v f="$tgz" -v s="$sha" '$2==f{found=1; ok=($1==s)} END{exit !(found&&ok)}' \
    || die "$name: release checksums file disagrees with the pinned sha256 for $tgz"
  mkdir -p "$(dirname "$dest")"
  tar -xzf "$work/$tgz" -C "$work" "$member"
  install -m 0755 "$work/$member" "$dest.new"
  mv -f "$dest.new" "$dest"
  log "$name installed: $dest"
}

fetch_unpack gc "$GC_TGZ" "$GC_SHA" "$GC_URL" gc "$GC_NEW" "gascity_${GC_VER}_checksums.txt"
fetch_unpack bd "$BD_TGZ" "$BD_SHA" "$BD_URL" bd "$BD_NEW" checksums.txt

step "installed versions"
"$GC_NEW" version
"$BD_NEW" version 2>&1 | head -1
