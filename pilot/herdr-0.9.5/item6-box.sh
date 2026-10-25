#!/bin/sh
# gcs-78y item 6, INSIDE the pilot box: phone/web access to herdr agents, without the city.
# A bare herdr server + the Ovis plugin (devswha/ovis, herdr web ui) pinned by commit. Nothing here needs a Claude login.
# Run inside the box:  sh item6-box.sh prep | server | install | agent | resume | status | uninstall | stop
#
# Exposure model: Ovis binds 0.0.0.0:7317 INSIDE the container (a loopback bind is unreachable through docker's port publish);
# pilot.sh publishes it as 127.0.0.1:7317 on the host only, and the host's existing `tailscale serve` route
# (https://<host>.ts.net -> 127.0.0.1:7317, tailnet only, no funnel) is the only way on. HERDR_WEB_TOKEN is set, so every
# client needs the token, whatever its address or Tailscale login. The token lives only in the box volume.
set -eu

cfg=$HOME/.config/pilot
ovis_ref=7e12a892273b5d7a2f61c181c95a2bb0ad178309   # tag v0.4.6, 2026-10-10
ovis_id=devswha.herdr-web-ui

cmd_prep() {
  mkdir -p "$cfg"; chmod 700 "$cfg"
  if [ ! -s "$cfg/ovis-token" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(24))' > "$cfg/ovis-token")
  fi
  if [ ! -e "$cfg/ovis-server.sh" ]; then
    (umask 077; cat > "$cfg/ovis-server.sh" <<'EOF'
#!/bin/sh
# herdr server with the Ovis environment. HOST/PORT are Ovis's own variable names.
export HOST=0.0.0.0 PORT=7317 HERDR_WEB_TELEMETRY=0 DO_NOT_TRACK=1
HERDR_WEB_TOKEN=$(cat "$HOME/.config/pilot/ovis-token"); export HERDR_WEB_TOKEN
exec herdr server
EOF
    )
    chmod 700 "$cfg/ovis-server.sh"
  fi
  echo "prep ok (token $(wc -c < "$cfg/ovis-token") bytes, mode $(stat -c %a "$cfg/ovis-token"))"
}

cmd_server() {
  # `herdr status server` exits 0 even when it prints "status: not running", so test the text
  if ! herdr status server 2>&1 | grep -q 'not running'; then echo "herdr server already running"; return 0; fi
  setsid nohup "$cfg/ovis-server.sh" > "$cfg/herdr-server.log" 2>&1 < /dev/null &
  for _ in 1 2 3 4 5 6 7 8 9 10; do herdr status server 2>&1 | grep -q 'not running' || break; sleep 1; done
  herdr status 2>&1 | head -6
}

cmd_install() {
  herdr plugin install devswha/ovis --ref "$ovis_ref" --yes
  herdr plugin list 2>&1 | head
}

# one workspace plus one not-logged-in claude pane so the web roster has an agent. The launch parks at the Bypass Permissions
# dialog (herdr reports `blocked`), and `herdr agent start` exits non-zero for that on purpose.
cmd_agent() {
  # herdr restores the saved workspace and pane after a restart, but not the agent process
  if herdr agent list 2>&1 | grep -q '"name":"probe"'; then echo "agent probe exists"; return 0; fi
  if herdr workspace list 2>&1 | grep -q '"label":"item6"'; then
    pane=$(herdr pane list | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["panes"][0]["pane_id"])')
  else
    pane=$(herdr workspace create --cwd "$HOME/scratch-city" --label item6 | python3 -c 'import sys, json; print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])')
  fi
  herdr agent start probe --kind claude --pane "$pane" -- --dangerously-skip-permissions 2>&1 | head -c 300 || true
  echo
  herdr agent list 2>&1 | head -c 400
}

# after a kill: everything the box needs for the item 6 soak, in order
cmd_resume() { cmd_prep; cmd_server; cmd_agent; }

cmd_status() {
  herdr status 2>&1 | head -8
  herdr plugin list 2>&1 | head
  herdr plugin action list --plugin "$ovis_id" 2>&1 | head -12 || true
  ss -ltn 2>/dev/null | grep -E ':7317\b' || echo "nothing listening on 7317"
}

cmd_uninstall() { herdr plugin uninstall "$ovis_id"; }
cmd_stop() { herdr server stop 2>&1 || true; }

case ${1:-} in
  prep) cmd_prep ;;
  server) cmd_server ;;
  install) cmd_install ;;
  agent) cmd_agent ;;
  resume) cmd_resume ;;
  status) cmd_status ;;
  uninstall) cmd_uninstall ;;
  stop) cmd_stop ;;
  *) sed -n '2,3p' "$0"; exit 2 ;;
esac
