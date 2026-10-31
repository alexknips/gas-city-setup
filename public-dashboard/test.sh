#!/usr/bin/env bash
# Offline test for expose.sh: touches nothing on the machine, needs no root and no network.
#   ./public-dashboard/test.sh
# Renders the Caddyfile from placeholders, checks it is complete, validates it with Caddy when Caddy is
# installed, and checks that nothing in this directory carries a real address, hostname or secret.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fail=0
ok()   { printf '  ok   %s\n' "$*"; }
bad()  { printf '  FAIL %s\n' "$*"; fail=1; }

out=$(PD_BIND=192.0.2.10 PD_HOSTNAME=dash.example.com PD_UPSTREAM=http://127.0.0.1:8787 "$HERE/expose.sh" render)
grep -q '@@' <<<"$out" && bad "unfilled placeholder in the rendered Caddyfile" || ok "no unfilled placeholder"
grep -q '^dash.example.com {' <<<"$out" && ok "site block uses the hostname" || bad "hostname missing"
grep -q 'default_bind 192.0.2.10' <<<"$out" && ok "binds to one address, not the wildcard" || bad "default_bind missing"
grep -q 'basic_auth' <<<"$out" && ok "login gate present" || bad "basic_auth missing"
awk '/basic_auth/{a=NR} /reverse_proxy/{r=NR} END{exit !(a && r && a<r)}' <<<"$out" && ok "login comes before the proxy" || bad "basic_auth must precede reverse_proxy"
PD_ACME=staging PD_BIND=192.0.2.10 PD_HOSTNAME=dash.example.com "$HERE/expose.sh" render | grep -q 'acme_ca https://acme-staging' && ok "PD_ACME=staging sets the test CA" || bad "PD_ACME=staging ignored"
PD_ACME=bogus PD_BIND=192.0.2.10 PD_HOSTNAME=dash.example.com "$HERE/expose.sh" render >/dev/null 2>&1 && bad "bad PD_ACME accepted" || ok "bad PD_ACME refused"

if command -v caddy >/dev/null 2>&1; then
  tmp=$(mktemp -d); trap "rm -rf -- '$tmp'" EXIT
  hash=$(printf '%s\n' "throwaway-test-value" | caddy hash-password)
  PD_HASH=$hash PD_BIND=192.0.2.10 PD_HOSTNAME=dash.example.com "$HERE/expose.sh" render >"$tmp/Caddyfile"
  if XDG_DATA_HOME=$tmp XDG_CONFIG_HOME=$tmp caddy validate --config "$tmp/Caddyfile" --adapter caddyfile >/dev/null 2>&1
  then ok "caddy validate accepts the rendered file"; else bad "caddy validate rejects the rendered file"; fi
else
  echo "  skip caddy validate (caddy not installed)"
fi

# This repository is public: only documentation-range addresses and example hostnames may appear here.
ips=$(grep -rhoE '\b[0-9]{1,3}(\.[0-9]{1,3}){3}\b' "$HERE" | grep -vE '^(192\.0\.2\.|127\.0\.0\.1$|1\.1\.1\.1$|0\.0\.0\.0$|203\.0\.113\.|198\.51\.100\.)' | sort -u || true)
[ -z "$ips" ] && ok "no real IP address in this directory" || bad "unexpected IP address(es): $ips"
grep -rnE '[a-z0-9-]+\.(ts\.net|sslip\.io)' "$HERE" | grep -vE 'ip-with-dashes|<' >/dev/null && bad "a concrete tailnet or sslip.io hostname is committed" || ok "no concrete hostname"
grep -rnE '^(password|hash)=' "$HERE" >/dev/null && bad "a credential line is committed" || ok "no credential"

exit "$fail"
