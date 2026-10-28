#!/usr/bin/env bash
# Fast, local end-to-end in a privileged systemd Docker container (no KVM needed).
#
#   tests/docker.sh up          # build the image, start the container, install autonomous
#   tests/docker.sh install     # copy this checkout + test config into a running container and install
#   tests/docker.sh scenario work   # run a calculator scenario on the host against the container's bridge
#   tests/docker.sh doctor|status|logs|shell
#   tests/docker.sh down        # remove the container (the image stays)
#   tests/docker.sh reset       # down + up
#   tests/docker.sh clean       # down + remove the image
#
# It is a container, not a VM: install.sh, systemd and loginctl work, but the kernel is shared with the host.
# The container is started with --privileged; use it for sandbox repos only, never with production secrets.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CONTAINER=${DOCKER_CONTAINER:-autonomous-docker-test}
IMAGE=${DOCKER_IMAGE:-autonomous-systemd-test}
HOST_ENV=${AUTONOMOUS_HOST_ENV:-/etc/autonomous/env}
REPO_A=${SANDBOX_REPO_A:-worotyns/autonomous-sandbox}
REPO_B=${SANDBOX_REPO_B:-worotyns/autonomous-sandbox-2}
DOCKERFILE=$ROOT/tests/docker/Dockerfile

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf 'docker.sh: %s\n' "$*" >&2; exit 1; }

# The current herdr session may predate the docker group; retry this one command through sg.
if ! docker info >/dev/null 2>&1; then
  if id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker && command -v sg >/dev/null; then
    exec sg docker -c "$(printf '%q ' "$0" "$@")"
  fi
  die "docker is not accessible; add $USER to the docker group and log in again, or run: sg docker -c '$0 $*'"
fi

require_container() {
  docker inspect "$CONTAINER" >/dev/null 2>&1 || die "no container $CONTAINER; run: tests/docker.sh up"
  [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ] || docker start "$CONTAINER" >/dev/null
}

make_test_config() {
  local out=$1
  install -d -m 700 "$out/config"
  sed -e "s|{{REPO_A}}|$REPO_A|g" -e "s|{{REPO_B}}|$REPO_B|g" \
    "$ROOT/tests/autonomous.test.toml.in" > "$out/config/autonomous.toml"
  if [ -r "$HOST_ENV" ]; then
    cp "$HOST_ENV" "$out/config/env"
  elif [ -r "$ROOT/config/env" ]; then
    cp "$ROOT/config/env" "$out/config/env"
  else
    die "cannot read $HOST_ENV (or config/env); secrets are needed for the test container"
  fi
  chmod 600 "$out/config/env"
}

start_container() {
  log "building $IMAGE"
  docker build -q -t "$IMAGE" "$ROOT/tests/docker" >/dev/null
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  log "starting $CONTAINER (privileged systemd container)"
  docker run -d --name "$CONTAINER" --hostname "$CONTAINER" \
    --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock \
    "$IMAGE" >/dev/null
  local i
  for i in $(seq 1 60); do
    if docker exec "$CONTAINER" systemctl is-system-running >/dev/null 2>&1; then break; fi
    sleep 1
  done
  docker exec "$CONTAINER" systemctl is-system-running --wait >/dev/null 2>&1 || true
  docker exec "$CONTAINER" systemctl is-system-running >/dev/null 2>&1 \
    || die "systemd did not start in $CONTAINER"
}

install_into_container() {
  local tmp; tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" EXIT
  tar -C "$ROOT" --exclude .git --exclude config/env --exclude config/autonomous.toml -cf - . | tar -C "$tmp" -xf -
  make_test_config "$tmp"
  log "copying this checkout + sandbox test config into $CONTAINER"
  docker exec "$CONTAINER" rm -rf /opt/autonomous
  docker cp "$tmp/." "$CONTAINER:/opt/autonomous" >/dev/null
  log "running install.sh in $CONTAINER (native speed; a few minutes)"
  docker exec "$CONTAINER" bash /opt/autonomous/install.sh
  log "installed; doctor:"
  docker exec "$CONTAINER" autonomous doctor || true
}

up() {
  start_container
  install_into_container
  cat <<EOF

Next:
  tests/docker.sh scenario work     # full start -> PR -> review -> /approve -> merge -> release
  tests/docker.sh shell             # ubuntu shell in the container
  tests/docker.sh logs              # gh-bridge + herdr logs
  tests/docker.sh down              # remove the container
EOF
}

case "${1:-help}" in
  up) up ;;
  install) require_container; install_into_container ;;
  scenario) shift; require_container; exec "$ROOT/tests/scenario.sh" "$@" ;;
  doctor) require_container; docker exec "$CONTAINER" autonomous doctor || true ;;
  status)
    require_container
    docker exec "$CONTAINER" sh -c 'systemctl is-system-running; echo; autonomous status' || true
    ;;
  logs) require_container; exec docker exec -it "$CONTAINER" journalctl -u gh-bridge -u herdr -f ;;
  shell) require_container; exec docker exec -it "$CONTAINER" bash ;;
  down) docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; log "removed $CONTAINER" ;;
  reset) docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; up ;;
  clean) docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; docker rmi "$IMAGE" >/dev/null 2>&1 || true; log "removed $CONTAINER and $IMAGE" ;;
  help|--help|-h) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
  *) die "usage: $0 up|install|scenario <name>|doctor|status|logs|shell|down|reset|clean" ;;
esac
