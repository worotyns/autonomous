# A systemd userland in a Docker container: enough for install.sh, loginctl and the systemd services.
# Run it with --privileged --cgroupns=host and /sys/fs/cgroup mounted rw (tests/docker.sh does).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session sudo iproute2 ca-certificates curl gnupg bubblewrap \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
