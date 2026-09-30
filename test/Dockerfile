# Clean fresh-VPS stand-in for the setup proof: a 24.04 base with systemd as PID 1
# (the Gas City supervisor is a systemd user unit) and only what a cloud image ships:
# curl, sudo, ca-certificates. Build: docker build --build-arg BASE=<image> -t gcs-test test
ARG BASE
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv libpam-systemd dbus dbus-user-session curl ca-certificates sudo \
 && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
