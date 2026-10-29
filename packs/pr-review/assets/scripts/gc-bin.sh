# Sourced by the order entry points: export GC_BIN, the gc binary this run calls. GC_BIN when set,
# else the gc that started the order (the nearest ancestor process whose binary is named gc),
# else gc on PATH. A cron or systemd PATH can leave out where Gas City's gc is installed and
# find Graphviz's /usr/bin/gc, which prints nothing and exits 0 (README, "Traps").
if [ -z "${GC_BIN:-}" ]; then
  p=$PPID
  for _ in 1 2 3 4 5 6 7 8; do
    [ "${p:-0}" -gt 1 ] 2>/dev/null || break
    e=$(readlink "/proc/$p/exe" 2>/dev/null) || break
    if [ "${e##*/}" = gc ]; then GC_BIN=$e; break; fi
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
  GC_BIN=${GC_BIN:-$(command -v gc || echo gc)}
  export GC_BIN
fi
