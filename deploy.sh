#!/usr/bin/env bash
# One command from your laptop: copy this repo to the VPS and run the installer there.
#
#   ./deploy.sh root@1.2.3.4 [--tailscale]
#   ./deploy.sh ubuntu@1.2.3.4               # a user with passwordless sudo works too
#   SSH_CONFIG=~/.lima/autonomous/ssh.config.host ./deploy.sh lima-autonomous   # local test VM
#
# First time: put your settings in config/autonomous.toml and config/env (copies of the *.example files,
# both gitignored). They are installed to /etc/autonomous on the first run only; later edit them on the VPS.
set -euo pipefail
HOST=${1:?usage: ./deploy.sh [user@]host [--tailscale]}; shift
cd "$(dirname "$0")"
SSH=(ssh); [ -n "${SSH_CONFIG:-}" ] && SSH+=(-F "$SSH_CONFIG")
[ -f config/env ] || echo "note: no config/env here, the VPS gets env.example and you fill it in there"

SUDO=$("${SSH[@]}" "$HOST" '[ "$(id -u)" = 0 ] || echo sudo')
"${SSH[@]}" "$HOST" "command -v rsync >/dev/null || ($SUDO apt-get update -qq && $SUDO apt-get install -y -qq rsync)"
rsync -az --delete --exclude .git --exclude '*.db' -e "${SSH[*]}" --rsync-path="$SUDO rsync" ./ "$HOST:/opt/autonomous/"
"${SSH[@]}" -t "$HOST" "$SUDO bash /opt/autonomous/install.sh $*"
