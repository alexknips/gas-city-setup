# Rehearsal box for the gc 1.4.1 -> 1.5.1 upgrade: Ubuntu 24.04 with systemd as PID 1 (the supervisor is a
# systemd user unit) and the live host's tool layout. gc, bd and dolt sit under the same linuxbrew paths as on
# the host (the live unit's ExecStart names .../Cellar/gascity/1.4.1/bin/gc), built from tools/ by rehearse.sh.
# Build through rehearse.sh (it assembles the tools/ context); not meant to be built by hand.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv libpam-systemd dbus dbus-user-session \
      curl ca-certificates sudo tmux jq git procps lsof iproute2 rsync python3 less time file xz-utils \
 && rm -rf /var/lib/apt/lists/*
COPY tools/ /home/linuxbrew/.linuxbrew/
COPY fake-claude /usr/local/bin/claude
RUN chmod 0755 /usr/local/bin/claude \
 && chown -R ubuntu:ubuntu /home/linuxbrew
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
