#!/usr/bin/env bash
# gcs: a generic Gas City on a fresh apt-based Linux box, in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/alexknips/gas-city-setup/main/setup.sh | bash
#
# Run as root (a sudo user is created first) or as a normal sudo user. Safe to re-run.
# Everything you may want to change lives in ~/.config/gcs/config.env; apply edits with `gcs setup`.
#   gcs setup              this script       gcs rig-add <path|url>   add a project to the city
# Components drop in as <dir>/ in the repo, no edits here: install.sh runs after the city is up
# (every setup), restore.sh runs before it when started with --restore, cmd.sh runs as `gcs <dir> ...`.
set -euo pipefail

GCS_REPO_URL=${GCS_REPO_URL:-https://github.com/alexknips/gas-city-setup.git}
GCS_BRANCH=${GCS_BRANCH:-main}
GC_VERSION=${GC_VERSION:-1.4.2}      # tested tool versions; override in the env to try others
BD_VERSION=${BD_VERSION:-1.3.0}
DOLT_VERSION=${DOLT_VERSION:-2.1.7}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

apt_install() {  # install only the missing packages; no apt call at all when nothing is missing
  local missing=() p
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  log "apt: installing ${missing[*]}"
  root apt-get update -qq >/dev/null
  root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
}

sync_clone() {  # sync_clone <dir>: clone the setup repo, or fast-forward an existing clone
  if [ -d "$1/.git" ]; then
    git -C "$1" pull -q --ff-only origin "$GCS_BRANCH" || warn "could not update $1; using the current copy"
  else
    git clone -q --depth 1 -b "$GCS_BRANCH" "$GCS_REPO_URL" "$1"
  fi
}

# ---- started as root on a brand-new box: make a sudo user, then continue as that user -----------
root_stage() {
  local user=${GCS_USER:-}
  [ -n "$user" ] || user=$(find /home -maxdepth 4 -path '*/.config/gcs/config.env' 2>/dev/null | head -1 | cut -d/ -f3)
  user=${user:-gc}
  log "running as root: using the sudo user '$user'"
  apt_install curl ca-certificates git sudo
  id -u "$user" >/dev/null 2>&1 || useradd -m -s /bin/bash "$user"
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$user" > "/etc/sudoers.d/90-gcs-$user"
  chmod 440 "/etc/sudoers.d/90-gcs-$user"
  local home; home=$(getent passwd "$user" | cut -d: -f6)
  if [ -s /root/.ssh/authorized_keys ]; then  # same ssh keys as root, so `ssh user@host` works
    install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
    touch "$home/.ssh/authorized_keys"
    local k
    while IFS= read -r k; do
      grep -qxF "$k" "$home/.ssh/authorized_keys" || printf '%s\n' "$k" >> "$home/.ssh/authorized_keys"
    done < /root/.ssh/authorized_keys
    chown "$user:$user" "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"
  fi
  local script=$home/.gcs/setup.sh pass=()
  if [ -n "${SELF_DIR:-}" ] && sudo -u "$user" test -r "$SELF_DIR/setup.sh"; then script=$SELF_DIR/setup.sh
  else sudo -H -u "$user" bash -c "$(declare -f sync_clone warn); GCS_BRANCH=$GCS_BRANCH GCS_REPO_URL=$GCS_REPO_URL; sync_clone \$HOME/.gcs"; fi
  while IFS= read -r k; do pass+=("$k"); done < <(env | grep -E '^(GCS_|GC_VERSION=|BD_VERSION=|DOLT_VERSION=)' || true)
  exec sudo -H -u "$user" env "${pass[@]}" GCS_FROM_ROOT=1 "GCS_T0=$T0" "GCS_REPO_URL=$GCS_REPO_URL" "GCS_BRANCH=$GCS_BRANCH" bash "$script" "$@"
}

# ---- config: ~/.config/gcs/config.env, seeded from the repo's annotated default -----------------
load_config() {
  GCS_CONFIG=${GCS_CONFIG:-$HOME/.config/gcs/config.env}
  if [ ! -f "$GCS_CONFIG" ]; then
    mkdir -p "$(dirname "$GCS_CONFIG")"
    cp "$GCS_HOME/config.env" "$GCS_CONFIG"
    sed -i "s/^GCS_USER=[^ ]*/GCS_USER=$(id -un)/" "$GCS_CONFIG"
    ok "wrote default config: $GCS_CONFIG"
  fi
  set -a
  # shellcheck source=/dev/null
  . "$GCS_CONFIG"
  set +a
  case "$MERGE_MODE" in pr|direct) ;; *) die "MERGE_MODE must be pr or direct (in $GCS_CONFIG)" ;; esac
  [[ $POLECAT_POOL =~ ^[0-9]+$ ]] || die "POLECAT_POOL must be a number (in $GCS_CONFIG)"
  local k; for k in MODEL_MAYOR MODEL_DEFAULT MODEL_POLECAT; do
    [[ ${!k} =~ ^(opus|sonnet|haiku)?$ ]] || die "$k must be opus, sonnet or haiku (in $GCS_CONFIG)"
  done
  CITY=$GC_CITY_DIR
}

# ---- machine: packages, swap, per-user systemd ---------------------------------------------------
ensure_swap() {
  [ "$SWAP" = off ] && return 0
  [ -n "$(swapon --noheadings --show=NAME 2>/dev/null)" ] && return 0
  local gb=$SWAP mem_gb; mem_gb=$(awk '/^MemTotal/{print int($2/1048576)}' /proc/meminfo)
  if [ "$SWAP" = auto ]; then [ "$mem_gb" -lt 16 ] || return 0; gb=4; fi
  log "swap: adding ${gb} GB swap file (RAM is ${mem_gb} GB)"
  if root fallocate -l "${gb}G" /swapfile && root chmod 600 /swapfile && root mkswap -q /swapfile && root swapon /swapfile; then
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | root tee -a /etc/fstab >/dev/null
  else
    warn "could not enable swap here (container?); continuing without"; root rm -f /swapfile
  fi
}

user_systemd() {  # the supervisor is a systemd user unit: linger keeps it alive after logout
  have systemctl || return 1
  root loginctl enable-linger "$(id -un)" 2>/dev/null || true
  XDG_RUNTIME_DIR=/run/user/$(id -u); export XDG_RUNTIME_DIR
  local i; for i in $(seq 30); do [ -S "$XDG_RUNTIME_DIR/bus" ] && break; sleep 1; done
  export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
  systemctl --user show-environment >/dev/null 2>&1
}

fetch_bin() {  # fetch_bin <name> <version> <url> <archive member> [<checksums url>]
  local name=$1 ver=$2 url=$3 member=$4 sums=${5:-} tmp want have_ver=""
  have "$name" && have_ver=$("$name" version 2>/dev/null || true)  # not piped to grep -q: pipefail + SIGPIPE
  if [[ $have_ver == *"$ver"* ]]; then ok "$name $ver"; return 0; fi
  log "installing $name $ver"
  tmp=$(mktemp -d)
  curl -fsSL --retry 3 -o "$tmp/pkg.tgz" "$url" || die "download failed: $url"
  if [ -n "$sums" ]; then
    want=$(curl -fsSL "$sums" | awk -v f="${url##*/}" '$2==f{print $1}')
    [ -n "$want" ] && [ "$(sha256sum "$tmp/pkg.tgz" | cut -d' ' -f1)" = "$want" ] || die "checksum mismatch for $name"
  fi
  tar -xzf "$tmp/pkg.tgz" -C "$tmp" "$member"
  install -m 755 "$tmp/$member" "$BIN/$name"
  rm -rf "$tmp"
}

dcg_hook_ok() { jq -e '[.hooks.PreToolUse[]?.hooks[]?.command | select(type == "string" and test("dcg$"))] | length > 0' "$HOME/.claude/settings.json" >/dev/null 2>&1; }

install_tools() {
  local gh=https://github.com
  apt_install git tmux jq curl ca-certificates python3 lsof procps openssh-client gh xz-utils minisign
  fetch_bin dolt "$DOLT_VERSION" "$gh/dolthub/dolt/releases/download/v$DOLT_VERSION/dolt-linux-$ARCH.tar.gz" "dolt-linux-$ARCH/bin/dolt"
  fetch_bin bd "$BD_VERSION" "$gh/gastownhall/beads/releases/download/v$BD_VERSION/beads_${BD_VERSION}_linux_$ARCH.tar.gz" bd \
    "$gh/gastownhall/beads/releases/download/v$BD_VERSION/checksums.txt"
  fetch_bin gc "$GC_VERSION" "$gh/gastownhall/gascity/releases/download/v$GC_VERSION/gascity_${GC_VERSION}_linux_$ARCH.tar.gz" gc \
    "$gh/gastownhall/gascity/releases/download/v$GC_VERSION/gascity_${GC_VERSION}_checksums.txt"
  if have claude; then ok "claude $(claude --version 2>/dev/null | head -1)"; else log "installing Claude Code"; curl -fsSL https://claude.ai/install.sh | bash; fi
  if have dcg; then ok "dcg"; else log "installing dcg"; curl -fsSL "https://raw.githubusercontent.com/Dicklesworthstone/destructive_command_guard/main/install.sh" | bash -s -- --easy-mode --no-gum; fi
  dcg_hook_ok || dcg install || true  # dcg merges its PreToolUse hook into ~/.claude/settings.json (keeps the rest)
  dcg_hook_ok || die "dcg hook is not in ~/.claude/settings.json; run: dcg install"
  ok "dcg hook registered in ~/.claude/settings.json"
  if have caam; then ok "caam"; else log "installing caam"; curl -fsSL "https://raw.githubusercontent.com/Dicklesworthstone/coding_agent_account_manager/main/install.sh" | bash; fi
  if [ "$TAILSCALE" = yes ] && ! have tailscale; then log "installing tailscale"; curl -fsSL https://tailscale.com/install.sh | root sh; fi
  have gcs && [ "$(readlink -f "$BIN/gcs")" = "$GCS_HOME/setup.sh" ] || ln -sfn "$GCS_HOME/setup.sh" "$BIN/gcs"
  # shellcheck disable=SC2016  # the $HOME must reach ~/.profile unexpanded
  grep -qs '\.local/bin' "$HOME/.profile" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.profile"
}

# ---- the city ---------------------------------------------------------------------------------------
put_if_changed() {  # put_if_changed <file> <content>: rewrite the file only when it differs; sets CHANGED
  local cur=""
  [ -f "$1" ] && cur=$(cat "$1")
  [ "$cur" = "$2" ] && return 0
  printf '%s\n' "$2" > "$1"; CHANGED=1
}

apply_config() {  # push config.env into the city; gc rewrites city.toml itself, so nothing here depends on its comments
  local f=$CITY/city.toml rig frag
  # gcs.toml (pulled in by city.toml's include) is generated: the mayor model, and pool + model per rig
  frag="# Generated by gcs setup from $GCS_CONFIG. Edit that file and run: gcs setup"$'\n'
  frag+=$(printf '[[patches.agent]]\ndir = ""\nname = "mayor"\n[patches.agent.option_defaults]\nmodel = "%s"' "$MODEL_MAYOR")
  if [ -f "$f" ]; then
    while IFS= read -r rig; do
      frag+=$(printf '\n\n[[patches.agent]]\ndir = "%s"\nname = "polecat"\nmax_active_sessions = %s\n[patches.agent.option_defaults]\nmodel = "%s"' \
        "$rig" "$POLECAT_POOL" "$MODEL_POLECAT")
    done < <(awk '/^\[\[rigs\]\]/{r=1; next} r&&/^name *=/{gsub(/^name *= *"|"$/, ""); print; r=0}' "$f")
  fi
  put_if_changed "$CITY/gcs.toml" "$frag"
  [ -f "$f" ] || return 0
  grep -q '^include *=' "$f" || { sed -i '1i include = ["gcs.toml"]' "$f"; CHANGED=1; }
  # the house model is the one value that lives in city.toml itself (a fragment cannot extend the provider)
  put_if_changed "$f" "$(awk -v m="$MODEL_DEFAULT" '/^\[/{t=$0} t=="[providers.claude.option_defaults]" && /^model *=/{print "model = \"" m "\""; next} {print}' "$f")"
  return 0
}

merge_mode_formula() {  # pr is one word in the refinery patrol formula: its fallback for merge_strategy
  local dst=$CITY/formulas/mol-refinery-patrol.toml sha tag tmp
  if [ "$MERGE_MODE" = direct ]; then rm -f "$dst"; return 0; fi
  sha=$(awk '/imports.gastown/{f=1} f&&/version/{gsub(/.*sha:|"/, ""); print; exit}' "$CITY/pack.toml")
  tag="# gcs: mol-refinery-patrol from gastown@$sha, merge_strategy fallback \"$MERGE_MODE\""
  [ "$(head -n1 "$dst" 2>/dev/null)" = "$tag" ] && return 0
  tmp=$(mktemp)
  curl -fsSL "https://raw.githubusercontent.com/gastownhall/gascity-packs/$sha/gastown/formulas/mol-refinery-patrol.toml" -o "$tmp" \
    || die "could not fetch the refinery formula"
  grep -q 'metadata.merge_strategy // "direct"' "$tmp" || die "refinery formula changed upstream; update merge_mode_formula in setup.sh"
  { echo "$tag"; sed 's@\.metadata\.merge_strategy // "direct"@.metadata.merge_strategy // "'"$MERGE_MODE"'"@' "$tmp"; } > "$dst"
  rm -f "$tmp"; log "merge mode: $MERGE_MODE (city formula override)"
}

supervisor_env() {  # the supervisor, and so every agent it starts, needs the bd consent variable too
  local d=$HOME/.config/systemd/user/gascity-supervisor.service.d
  [ -f "$d/gcs.conf" ] && return 0
  mkdir -p "$d"
  printf '[Service]\nEnvironment=BD_ALLOW_REMOTE_MIGRATE=1\n' > "$d/gcs.conf"
  systemctl --user daemon-reload
}

create_city() {
  local name email first=0
  CHANGED=0
  { [ -f "$CITY/city.toml" ] && [ -d "$CITY/.gc" ]; } || first=1
  name=$(git config --global user.name || true); email=$(git config --global user.email || true)
  [ -n "$name" ] || { git config --global user.name "$GIT_NAME"; name=$GIT_NAME; }
  [ -n "$email" ] || { git config --global user.email "$GIT_EMAIL"; email=$GIT_EMAIL; }
  dolt config --global --get user.name >/dev/null 2>&1 || dolt config --global --add user.name "$name" >/dev/null
  dolt config --global --get user.email >/dev/null 2>&1 || dolt config --global --add user.email "$email" >/dev/null
  mkdir -p "$CITY"
  [ -f "$CITY/pack.toml" ] || cp "$GCS_HOME/pack.toml" "$CITY/pack.toml"
  if [ "$first" = 1 ]; then
    log "creating the city in $CITY"
    gc init --file "$GCS_HOME/city.toml" --preserve-existing --no-start --skip-provider-readiness "$CITY"
  fi
  apply_config
  # claude asks "trust this folder?" per directory; an untrusted city dir stalls every agent on it
  trust_dir "$CITY"
  [ -f "$CITY/packs.lock" ] || ( cd "$CITY" && gc import install )
  merge_mode_formula
  if user_systemd; then
    systemctl --user cat gascity-supervisor.service >/dev/null 2>&1 || gc supervisor install >/dev/null
    supervisor_env
  else warn "no systemd user session: the supervisor will not survive a reboot"; fi
  # Agents started before `claude` is logged in would sit on its login screen: keep the city paused
  # until it is (the next `gcs setup` resumes it).
  AWAIT=$(dirname "$GCS_CONFIG")/awaiting-login
  if [ "$first" = 1 ] && ! claude auth status >/dev/null 2>&1; then gc suspend "$CITY" >/dev/null && touch "$AWAIT"; fi
  gc register "$CITY" >/dev/null
  if [ -f "$AWAIT" ] && claude auth status >/dev/null 2>&1; then gc --city "$CITY" resume >/dev/null && rm -f "$AWAIT" && log "claude is logged in: city resumed"; fi
  [ "$CHANGED" = 0 ] || gc --city "$CITY" reload >/dev/null 2>&1 || true
  # so `gc` finds the city from any directory in a login shell
  grep -qxF "export GC_CITY=\"$CITY\"" "$HOME/.profile" || { sed -i '/^export GC_CITY=/d' "$HOME/.profile"; echo "export GC_CITY=\"$CITY\"" >> "$HOME/.profile"; }
  ok "city: $CITY"
}

run_components() {
  local d failed=()
  if [ -n "${GCS_RESTORE:-}" ]; then
    for d in "$GCS_HOME"/*/restore.sh; do [ -f "$d" ] && { log "restore: $(basename "$(dirname "$d")")"; bash "$d"; }; done
  fi
  for d in "$GCS_HOME"/*/install.sh; do
    [ -f "$d" ] || continue
    log "component: $(basename "$(dirname "$d")")"
    bash "$d" || { warn "component failed: $d"; failed+=("$d"); }
  done
  [ ${#failed[@]} -eq 0 ] || die "failed components: ${failed[*]}"
}

logins() {
  local n=0 secs=$(( $(date +%s) - T0 ))
  printf '\n\033[1;32mGas City is up\033[0m in %dm%02ds  (city: %s)\n' $((secs / 60)) $((secs % 60)) "$CITY"
  gc --city "$CITY" status 2>&1 | head -12 || true
  [ -z "${GCS_FROM_ROOT:-}" ] || printf '\nSwitch to the new user first:  su - %s   (or: ssh %s@<this-server>)\n' "$(id -un)" "$(id -un)"
  printf '\nNow log in (once). Anything already done is not listed:\n'
  claude auth status >/dev/null 2>&1 || { n=$((n+1)); printf '  %d. claude              # sign in with your Claude account, then exit\n' $n; }
  gh auth status >/dev/null 2>&1 || { n=$((n+1)); printf '  %d. gh auth login       # GitHub, needed to open PRs\n' $n; }
  [ "$TAILSCALE" = yes ] && { n=$((n+1)); printf '  %d. sudo tailscale up   # join your tailnet\n' $n; }
  n=$((n+1)); printf '  %d. caam backup claude main   # after the claude login: saves it, so you can switch accounts at a limit\n' $n
  [ ! -f "$AWAIT" ] || { n=$((n+1)); printf '  %d. gcs setup           # the city is paused until claude is logged in; this resumes it\n' $n; }
  printf '\nThen add a project:  gcs rig-add https://github.com/<you>/<repo>\n'
  printf 'Open the dashboard:  gc dashboard --no-open   (over ssh: ssh -L <port>:127.0.0.1:<port> %s@<this-server>)\n' "$(id -un)"
}

cmd_setup() {
  while [ $# -gt 0 ]; do
    case $1 in --restore) GCS_RESTORE=${2:?--restore needs an archive or url}; export GCS_RESTORE; shift 2 ;; *) die "unknown option: $1" ;; esac
  done
  have apt-get || die "this script needs an apt-based Linux (Debian family)"
  have sudo || die "sudo is required (or start this as root)"
  load_config
  log "config: $GCS_CONFIG"
  ensure_swap
  install_tools
  create_city
  run_components
  logins
}

# ---- gcs rig-add <path|url>: `gc rig add` plus what a rig needs to actually work ------------------------------
trust_dir() {  # mark a directory trusted in ~/.claude.json (backup first, atomic write)
  local f=$HOME/.claude.json d=$1 tmp
  [ -f "$f" ] || ( umask 077; echo '{}' > "$f" )
  jq -e --arg d "$d" '.projects[$d].hasTrustDialogAccepted == true' "$f" >/dev/null 2>&1 && return 0
  jq -e . "$f" >/dev/null 2>&1 || die "$f is not valid JSON; fix it, then re-run"
  cp -p "$f" "$f.gcs-bak.$(date +%s)"
  tmp=$(mktemp "$f.XXXXXX")
  jq --arg d "$d" '.projects[$d].hasTrustDialogAccepted = true' "$f" > "$tmp" && mv "$tmp" "$f"
  ok "claude trusts $d"
}

rig_listed() { grep -qE "^name = \"$1\"\$" "$CITY/city.toml"; }

cmd_rig_add() {
  local src=${1:-} path root name db i a prev="" adopt=()
  [ -n "$src" ] || die "usage: gcs rig-add <path|git-url> [gc rig add flags]"
  shift
  load_config
  case "$src" in
    http://*|https://*|git@*|ssh://*|*.git)
      path=$GCS_REPOS_DIR/$(basename "${src%.git}")
      [ -d "$path/.git" ] || git clone "$src" "$path" ;;
    */*) if [ -d "$src" ]; then path=$src; else path=$GCS_REPOS_DIR/${src#*/}; [ -d "$path/.git" ] || gh repo clone "$src" "$path"; fi ;;
    *) path=$src ;;
  esac
  root=$(git -C "$path" rev-parse --show-toplevel) || die "$path is not a git repository"
  name=$(basename "$root")
  for a in "$@"; do  # a --name flag changes the rig's name
    [ "$prev" = --name ] && name=$a
    case $a in --name=*) name=${a#--name=} ;; esac
    prev=$a
  done
  # claude resolves a polecat worktree to the repo root; if that is untrusted every polecat dies on the dialog
  trust_dir "$root"
  if rig_listed "$name"; then ok "rig $name is already in the city"; return 0; fi
  for i in 1 2 3 4 5; do
    ( cd "$CITY" && gc rig add "$root" "${adopt[@]}" "$@" ) || true
    rig_listed "$name" && break
    # beads#4566: the new rig database is born with an uncommitted working set and refuses to migrate.
    # Commit it, then adopt the half-made rig on the next round.
    db=$(jq -r '.dolt_database // empty' "$root/.beads/metadata.json" 2>/dev/null || true)
    [ -n "$db" ] || die "gc rig add failed (reason above); nothing was created in $root"
    warn "rig add round $i failed; committing the new database's working set and retrying"
    ( cd "$CITY" && gc dolt sql -q "USE \`$db\`; CALL DOLT_ADD('-A'); CALL DOLT_COMMIT('-m', 'gcs: initial rig schema', '--allow-empty');" ) || true
    adopt=(--adopt)
  done
  rig_listed "$name" || die "rig $name did not appear in $CITY/city.toml after $i rounds"
  CHANGED=0; apply_config  # gives the new rig its polecat pool and model
  [ "$CHANGED" = 0 ] || gc --city "$CITY" reload >/dev/null 2>&1 || true
  ok "rig $name added (polecats: $POLECAT_POOL x $MODEL_POLECAT, merge mode: $MERGE_MODE)"
}

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

main() {
  T0=${GCS_T0:-$(date +%s)}
  case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) die "unsupported CPU: $(uname -m)" ;; esac
  SELF_DIR=""
  if [ -f "${BASH_SOURCE[0]:-}" ]; then SELF_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd); fi
  [ -f "$SELF_DIR/city.toml" ] || SELF_DIR=""
  if [ "$(id -u)" -eq 0 ]; then root_stage "$@"; fi
  BIN=$HOME/.local/bin; mkdir -p "$BIN"; export PATH="$BIN:$PATH"
  # bd 1.3 refuses to migrate a fresh shared-server database (bd init stops at schema v22 of v66, #5920)
  # unless told this is the only client. It is: one bd version per city.
  export BD_ALLOW_REMOTE_MIGRATE=1
  GCS_HOME=${SELF_DIR:-$HOME/.gcs}
  if [ -z "$SELF_DIR" ]; then  # piped from curl: fetch the repo, then run the copy that lives in it
    have git || apt_install git
    sync_clone "$GCS_HOME"
    export GCS_T0=$T0
    exec bash "$GCS_HOME/setup.sh" "$@"
  fi
  case "${1:-setup}" in
    -h|--help|help) usage ;;
    setup) shift || true; cmd_setup "$@" ;;
    -*) cmd_setup "$@" ;;  # e.g. --restore <archive>
    rig-add) shift; cmd_rig_add "$@" ;;
    *) if [ -f "$GCS_HOME/$1/cmd.sh" ]; then load_config; sub=$1; shift; exec bash "$GCS_HOME/$sub/cmd.sh" "$@"; fi
       usage; exit 2 ;;
  esac
}

main "$@" </dev/null
