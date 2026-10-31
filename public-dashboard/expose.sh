#!/usr/bin/env bash
# expose.sh: put a read-only dashboard on the public internet over HTTPS, behind a login, with Caddy
# running on this box (no VPN, no tunnel or proxy service in the data path). See README.md in this directory.
#
#   sudo PD_UPSTREAM=http://127.0.0.1:8787 ./expose.sh [setup]   install Caddy, write config, start, wait for the certificate
#   sudo ./expose.sh check      prove it from this box: valid certificate, login required, full page loads, writes refused
#   sudo ./expose.sh rotate     new random password (the old one stops working at once)
#   sudo ./expose.sh remove     take it off the internet: stop and disable Caddy, delete the credentials
#   ./expose.sh render          print the Caddyfile that would be written (placeholder password hash; touches nothing)
#
# Deliberately NOT named install.sh: setup.sh runs every <dir>/install.sh on every setup, and publishing a
# dashboard must never happen by itself. Run this by hand, once, on the box that serves the dashboard.
#
# Settings (environment; none of them is stored in this repository):
#   PD_UPSTREAM   the dashboard to front, default http://127.0.0.1:8787 (any address Caddy can reach)
#   PD_USER       the one login name, default dashboard
#   PD_HOSTNAME   public name, default <public-ip-with-dashes>.sslip.io (sslip.io only RESOLVES the name)
#   PD_BIND       address Caddy listens on (ports 80 and 443), default the address of the default route.
#                 Never the wildcard: that would collide with a service already on <ip>:443 (tailscaled).
#   PD_PUBLIC_IP  the box's public IPv4, only used to derive PD_HOSTNAME; default PD_BIND if public, else asked
#                 of api.ipify.org once at setup
#   PD_ACME       production (default) | staging (untrusted test certificate, for a dry run) | a directory URL
#   PD_WAIT       seconds to wait for the certificate, default 180
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONF_DIR=/etc/caddy
CADDYFILE=$CONF_DIR/Caddyfile
CREDS=$CONF_DIR/dashboard.credentials

PD_UPSTREAM=${PD_UPSTREAM:-http://127.0.0.1:8787}
PD_USER=${PD_USER:-dashboard}
PD_ACME=${PD_ACME:-production}
PD_WAIT=${PD_WAIT:-180}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
need_root() { [ "$(id -u)" -eq 0 ] || exec sudo -E "${BASH_SOURCE[0]}" "$@"; }

is_public_v4() {
  local a b c d
  IFS=. read -r a b c d <<<"$1"
  [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  case $a in 0|10|127) return 1 ;; esac
  [ "$a" -eq 169 ] && [ "$b" -eq 254 ] && return 1
  [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ] && return 1
  [ "$a" -eq 192 ] && [ "$b" -eq 168 ] && return 1
  [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ] && return 1
  [ "$a" -ge 224 ] && return 1
  return 0
}

resolve_settings() {
  PD_BIND=${PD_BIND:-$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1); exit}}')}
  [ -n "$PD_BIND" ] || die "cannot find this box's address; set PD_BIND"
  if [ -z "${PD_HOSTNAME:-}" ]; then
    if [ -z "${PD_PUBLIC_IP:-}" ]; then
      if is_public_v4 "$PD_BIND"; then PD_PUBLIC_IP=$PD_BIND
      else PD_PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org) || die "cannot learn the public IP; set PD_PUBLIC_IP or PD_HOSTNAME"; fi
    fi
    is_public_v4 "$PD_PUBLIC_IP" || die "PD_PUBLIC_IP '$PD_PUBLIC_IP' is not a public IPv4 address"
    PD_HOSTNAME=${PD_PUBLIC_IP//./-}.sslip.io
  fi
  case $PD_ACME in
    production) ACME_CA_LINE="" ;;
    staging)    ACME_CA_LINE="acme_ca https://acme-staging-v02.api.letsencrypt.org/directory" ;;
    https://*)  ACME_CA_LINE="acme_ca $PD_ACME" ;;
    *) die "PD_ACME must be production, staging or a https:// directory URL" ;;
  esac
}

# render <password-hash>: the template with every @@PLACEHOLDER@@ filled in, on stdout.
render() {
  local t; t=$(<"$HERE/Caddyfile.template")
  shopt -u patsub_replacement 2>/dev/null || true   # bash 5.2: '&' in a replacement would be special
  t=${t//@@BIND_IP@@/$PD_BIND}
  t=${t//@@HOSTNAME@@/$PD_HOSTNAME}
  t=${t//@@UPSTREAM@@/$PD_UPSTREAM}
  t=${t//@@USER@@/$PD_USER}
  t=${t//@@PASSWORD_HASH@@/$1}
  t=${t//@@ACME_CA_LINE@@/$ACME_CA_LINE}
  printf '%s\n' "$t"
}

install_caddy() {
  if have caddy; then ok "caddy $(caddy version | awk '{print $1}')"; return; fi
  have apt-get || die "this script installs Caddy with apt (Ubuntu/Debian); install Caddy yourself and re-run"
  log "installing Caddy from the official Caddy apt repository"
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg >/dev/null
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt >/etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y caddy >/dev/null
  ok "caddy $(caddy version | awk '{print $1}')"
}

cred() { awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,""); print; exit}' "$CREDS"; }

# The password is 40 random letters and digits; only its bcrypt hash goes into the Caddyfile.
make_credentials() {
  local pw hash
  pw=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 || true)
  [ "${#pw}" -eq 40 ] || die "could not generate a password"
  hash=$(printf '%s\n' "$pw" | caddy hash-password) || die "caddy hash-password failed"
  case $hash in '$2'*) ;; *) die "unexpected output from caddy hash-password" ;; esac
  umask 077
  { printf 'url=https://%s/\nuser=%s\npassword=%s\nhash=%s\n' "$PD_HOSTNAME" "$PD_USER" "$pw" "$hash"; } >"$CREDS.new"
  mv "$CREDS.new" "$CREDS"
  chmod 600 "$CREDS"; chown root:root "$CREDS"
}

cmd_setup() {
  need_root setup
  resolve_settings
  install_caddy
  if [ "${PD_ROTATE:-0}" = 1 ] || [ ! -s "$CREDS" ] || [ "$(cred user)" != "$PD_USER" ] || [ "$(cred url)" != "https://$PD_HOSTNAME/" ]; then
    log "generating the login"; make_credentials
  else ok "keeping the existing login ($CREDS)"; fi

  log "writing $CADDYFILE"
  local tmp; tmp=$(mktemp -d); trap "rm -rf -- '$tmp'" EXIT
  render "$(cred hash)" >"$tmp/Caddyfile"
  caddy fmt --overwrite "$tmp/Caddyfile" >/dev/null
  XDG_DATA_HOME=$tmp XDG_CONFIG_HOME=$tmp caddy validate --config "$tmp/Caddyfile" --adapter caddyfile >/dev/null 2>"$tmp/validate.err" \
    || { cat "$tmp/validate.err" >&2; die "the rendered Caddyfile does not validate"; }
  local changed=1
  [ -f "$CADDYFILE" ] && cmp -s "$tmp/Caddyfile" "$CADDYFILE" && changed=0
  if [ "$changed" = 1 ]; then
    install -m 0640 -o root -g caddy "$tmp/Caddyfile" "$CADDYFILE"
  fi

  # The packaged unit does not restart Caddy after a crash; boot start and the network-online ordering it ships are fine.
  local dropin=$tmp/restart.conf dropin_dir=/etc/systemd/system/caddy.service.d
  printf '[Service]\nRestart=on-failure\nRestartSec=5\n' >"$dropin"
  if ! cmp -s "$dropin" "$dropin_dir/restart.conf" 2>/dev/null; then
    install -d "$dropin_dir"; install -m 0644 "$dropin" "$dropin_dir/restart.conf"; systemctl daemon-reload; ok "caddy restarts after a failure"
  fi

  systemctl enable caddy >/dev/null 2>&1
  if systemctl is-active --quiet caddy; then
    [ "$changed" = 1 ] && { systemctl restart caddy; ok "caddy restarted with the new config"; } || ok "caddy already running this config"
  else systemctl start caddy; ok "caddy started"; fi
  systemctl is-enabled --quiet caddy && ok "caddy is enabled at boot"

  log "waiting for the certificate for $PD_HOSTNAME (up to ${PD_WAIT}s)"
  local code curlopt=(); [ "$PD_ACME" = production ] || curlopt=(-k)
  local deadline=$((SECONDS + PD_WAIT))
  while :; do
    code=$(curl -sS "${curlopt[@]}" -o /dev/null -w '%{http_code}' --max-time 10 "https://$PD_HOSTNAME/" 2>/dev/null || true)
    [ "$code" = 401 ] && break
    [ "$SECONDS" -ge "$deadline" ] && { journalctl -u caddy -n 40 --no-pager 2>/dev/null | grep -iE 'error|obtain|challenge|rate' | tail -15 >&2 || true; die "no working HTTPS after ${PD_WAIT}s (see journalctl -u caddy)"; }
    sleep 3
  done
  ok "https://$PD_HOSTNAME/ answers and asks for the login"
  [ "$PD_ACME" = production ] || warn "PD_ACME=$PD_ACME: the certificate is a test one, browsers will not trust it; re-run without PD_ACME for the real one"
  echo
  echo "URL:         https://$PD_HOSTNAME/"
  echo "login:       $PD_USER   (password: sudo cat $CREDS)"
  echo "prove it:    sudo $0 check"
}

cmd_check() {
  need_root check
  [ -s "$CREDS" ] || die "no $CREDS: run setup first"
  local url user host fails=0; url=$(cred url); user=$(cred user); host=${url#https://}; host=${host%/}
  local bind; bind=$(awk '/default_bind/{print $2; exit}' "$CADDYFILE")
  local pw; pw=$(cred password)
  # credentials go to curl on stdin, never into its argument list
  authcfg() { printf 'user = "%s:%s"\n' "$user" "$1"; }
  hc() { curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$@" 2>/dev/null || true; }
  expect() { # expect <name> <wanted> <got>
    if [ "$3" = "$2" ]; then ok "$1 -> $3"; else printf '  \033[1;31mFAIL\033[0m %s -> %s (wanted %s)\n' "$1" "$3" "$2"; fails=$((fails+1)); fi
  }
  log "checking $url"
  expect "valid certificate, no login"            401 "$(hc "$url")"
  expect "wrong password"                         401 "$(authcfg wrong-password | hc -K - "$url")"
  expect "right password, /"                      200 "$(authcfg "$pw" | hc -K - "$url")"
  for p in ${PD_CHECK_PATHS:-}; do expect "right password, $p" 200 "$(authcfg "$pw" | hc -K - "$url${p#/}")"; done
  expect "no login on a deep path"                401 "$(hc "${url}anything/at/all.json")"
  expect "POST with the right password"           405 "$(authcfg "$pw" | hc -K - -X POST -d x "$url")"
  expect "plain HTTP redirects to HTTPS"          308 "$(hc "http://$host/")"
  local h; h=$(hc -k "https://$bind/")
  [ "$h" != 200 ] && ok "bare IP over HTTPS serves nothing ($h)" || { printf '  \033[1;31mFAIL\033[0m bare IP over HTTPS serves content\n'; fails=$((fails+1)); }
  h=$(hc "http://$bind/")
  [ "$h" != 200 ] || [ -z "$(curl -sS --max-time 10 "http://$bind/" 2>/dev/null)" ] && ok "bare IP over HTTP serves no content ($h)" || { printf '  \033[1;31mFAIL\033[0m bare IP over HTTP serves content\n'; fails=$((fails+1)); }
  systemctl is-enabled --quiet caddy && ok "caddy is enabled at boot" || { printf '  \033[1;31mFAIL\033[0m caddy is not enabled at boot\n'; fails=$((fails+1)); }
  [ "$fails" -eq 0 ] || die "$fails check(s) failed"
  ok "all checks passed"
}

cmd_rotate() { PD_ROTATE=1 cmd_setup; }

cmd_remove() {
  need_root remove
  log "taking the dashboard off the internet"
  systemctl disable --now caddy 2>/dev/null || true
  rm -f "$CREDS" "$CREDS.new"
  ok "caddy stopped and disabled, credentials deleted ($CADDYFILE is left; the package is not removed)"
}

cmd_render() {
  PD_ACME=${PD_ACME:-production}
  PD_BIND=${PD_BIND:-192.0.2.10}; PD_HOSTNAME=${PD_HOSTNAME:-dashboard.example.com}
  resolve_settings
  render "${PD_HASH:-\$2a\$14\$exampleexampleexampleexampleexampleexampleexampleexample}"
}

case ${1:-setup} in
  setup)  cmd_setup ;;
  check)  cmd_check ;;
  rotate) cmd_rotate ;;
  remove) cmd_remove ;;
  render) cmd_render ;;
  *) die "usage: $0 [setup|check|rotate|remove|render]" ;;
esac
