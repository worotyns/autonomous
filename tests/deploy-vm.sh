#!/usr/bin/env bash
# Deploy this checkout with the sandbox test config to an already-running VM.
#
#   SSH_CONFIG=~/.lima/autonomous/ssh.config.host tests/deploy-vm.sh lima-autonomous
#   SSH_CONFIG=~/.autonomous-vm/ssh.config tests/deploy-vm.sh autonomous-vm
#
# It refuses to target this host: the test config replaces /etc/autonomous and contains only the sandbox repos.
# Force with ALLOW_LIVE_HOST=1 if you really mean it.
set -euo pipefail

HOST=${1:?usage: SSH_CONFIG=<file> tests/deploy-vm.sh <ssh-host> [deploy.sh flags]}
shift
ROOT=$(cd "$(dirname "$0")/.." && pwd)
HOST_ENV=${AUTONOMOUS_HOST_ENV:-/etc/autonomous/env}
REPO_A=${SANDBOX_REPO_A:-worotyns/autonomous-sandbox}
REPO_B=${SANDBOX_REPO_B:-worotyns/autonomous-sandbox-2}

die() { printf 'deploy-vm: %s\n' "$*" >&2; exit 1; }

case "$HOST" in
  localhost|127.0.0.1|"$(hostname)"|"$(hostname -f 2>/dev/null || hostname)")
    [ "${ALLOW_LIVE_HOST:-0}" = 1 ] || die "refusing to deploy the sandbox test config to the live host ($HOST)"
    ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
tar -C "$ROOT" --exclude .git --exclude config/env --exclude config/autonomous.toml -cf - . | tar -C "$tmp" -xf -
install -d -m 700 "$tmp/config"
sed -e "s|{{REPO_A}}|$REPO_A|g" -e "s|{{REPO_B}}|$REPO_B|g" \
  "$ROOT/tests/autonomous.test.toml.in" > "$tmp/config/autonomous.toml"
if [ -r "$HOST_ENV" ]; then
  cp "$HOST_ENV" "$tmp/config/env"
elif [ -r "$ROOT/config/env" ]; then
  cp "$ROOT/config/env" "$tmp/config/env"
else
  die "cannot read $HOST_ENV (or config/env); secrets are needed for the VM"
fi
chmod 600 "$tmp/config/env"

echo "deploying sandbox config ($REPO_A, $REPO_B) to $HOST"
( cd "$tmp" && SSH_CONFIG="${SSH_CONFIG:-}" ./deploy.sh "$HOST" "$@" )
