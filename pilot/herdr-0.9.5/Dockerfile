# Pilot box for gcs-78y: a scratch Gas City on gc 1.5.1 + herdr 0.9.5 (pinned), isolated from the live town.
# No systemd and no tmux session provider: one container, one supervisor, one herdr server. Everything that herdr
# spawns lives in this container's pid namespace, so the kill switch is `docker kill` and the live town's process
# table never sees a zombie. Build through pilot.sh (it stages ctx/ with the pinned binaries); credentials are never
# baked into the image.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates sudo tmux jq git procps lsof iproute2 \
      python3 less file xz-utils openssh-client \
 && rm -rf /var/lib/apt/lists/*
COPY ctx/gc ctx/bd ctx/dolt ctx/herdr ctx/claude /usr/local/bin/
RUN chmod 0755 /usr/local/bin/gc /usr/local/bin/bd /usr/local/bin/dolt /usr/local/bin/herdr /usr/local/bin/claude
USER ubuntu
ENV HOME=/home/ubuntu
WORKDIR /home/ubuntu
CMD ["sleep", "infinity"]
