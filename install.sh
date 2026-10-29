#!/usr/bin/env bash
# Idempotent VPS installer (Ubuntu 22.04+/Debian 12+, run as root from /opt/autonomous).
# Usually started by ./deploy.sh from your laptop. Safe to re-run: that is how repos and settings are updated.
#
#   sudo bash /opt/autonomous/install.sh [--tailscale]
set -euo pipefail

APP=/opt/autonomous
ETC=/etc/autonomous
STATE=/var/lib/autonomous
USER_NAME=agent
HOME_DIR=/home/$USER_NAME
WITH_TAILSCALE=0
for a in "$@"; do case "$a" in --tailscale) WITH_TAILSCALE=1 ;; *) echo "unknown flag $a"; exit 2 ;; esac; done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
as_agent() { sudo -u "$USER_NAME" -H env XDG_RUNTIME_DIR="/run/user/$(id -u $USER_NAME)" \
  PATH="$HOME_DIR/.herdr-projects/data/bin:$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  bash -lc "cd ~; set -a; [ -f $ETC/env ] && . $ETC/env; set +a; $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -f "$APP/bridge.py" ] || { echo "$APP/bridge.py missing: run ./deploy.sh from the repo"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

step "System packages"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg jq sqlite3 sudo build-essential rsync >/dev/null

if ! command -v gh >/dev/null; then
  step "GitHub CLI"
  install -d -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq && apt-get install -y -qq gh >/dev/null
fi

node_ok() { command -v node >/dev/null && node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit(a>22||(a==22&&b>=19)?0:1)'; }
if ! node_ok; then
  step "Node.js 22 (pi needs >= 22.19)"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi

if ! command -v uv >/dev/null; then
  step "uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

step "pi (coding agent)"
npm install -g --silent @earendil-works/pi-coding-agent@latest >/dev/null
pi --version || true

if [ "$WITH_TAILSCALE" = 1 ] && ! command -v tailscale >/dev/null; then
  step "Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
  echo "   then: tailscale up"
fi

step "User $USER_NAME"
id "$USER_NAME" >/dev/null 2>&1 || useradd -m -s /bin/bash "$USER_NAME"
loginctl enable-linger "$USER_NAME"
AGENT_UID=$(id -u "$USER_NAME")
for _ in $(seq 20); do [ -d /run/user/$AGENT_UID ] && break; sleep 0.5; done
grep -q herdr-projects/data/bin "$HOME_DIR/.profile" 2>/dev/null || \
  echo 'export PATH="$HOME/.herdr-projects/data/bin:$HOME/.local/bin:$PATH"; set -a; [ -r /etc/autonomous/env ] && . /etc/autonomous/env; set +a' >> "$HOME_DIR/.profile"

step "Config in $ETC"
install -d -m 750 -o root -g "$USER_NAME" "$ETC"
install -d -m 750 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
# First install takes your local config/ files if deploy.sh sent them, else the examples. Never overwrites.
copy_once() {
  local dst=$1; shift
  [ -f "$ETC/$dst" ] && return
  for s in "$@"; do
    [ -f "$APP/config/$s" ] || continue
    install -m 640 -o root -g "$USER_NAME" "$APP/config/$s" "$ETC/$dst"; echo "   wrote $ETC/$dst from config/$s"; return
  done
}
copy_once autonomous.toml autonomous.toml autonomous.example.toml
copy_once env env env.example
rm -f "$APP/config/env"   # the secret lives in $ETC only
set -a; . "$ETC/env"; set +a
MISSING=()
[ -n "${OPENROUTER_API_KEY:-}" ] || MISSING+=("OPENROUTER_API_KEY in $ETC/env")
[ -n "${GH_TOKEN:-}" ] || MISSING+=("GH_TOKEN in $ETC/env")
grep -q 'name = "acme/' "$ETC/autonomous.toml" && MISSING+=("your repos in $ETC/autonomous.toml (still acme/*)")

chown -R root:root "$APP"; chmod 755 "$APP" "$APP/bridge.py"

step "pi auth (OpenRouter)"
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.pi" "$HOME_DIR/.pi/agent"
  jq -n --arg k "$OPENROUTER_API_KEY" '{openrouter:{type:"api_key",key:$k}}' > "$HOME_DIR/.pi/agent/auth.json"
  chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.pi/agent/auth.json"; chmod 600 "$HOME_DIR/.pi/agent/auth.json"
fi

step "git + gh for $USER_NAME"
GIT_NAME=$(sed -n 's/^name *= *"\(.*\)"/\1/p' <(sed -n '/^\[git\]/,/^\[/p' "$ETC/autonomous.toml") | head -1)
GIT_EMAIL=$(sed -n 's/^email *= *"\(.*\)"/\1/p' <(sed -n '/^\[git\]/,/^\[/p' "$ETC/autonomous.toml") | head -1)
[ -n "$GIT_NAME" ] && as_agent "git config --global user.name '$GIT_NAME'"
[ -n "$GIT_EMAIL" ] && as_agent "git config --global user.email '$GIT_EMAIL'"
as_agent "git config --global init.defaultBranch main; git config --global pull.rebase false"
if [ -n "${GH_TOKEN:-}" ]; then
  as_agent "gh auth setup-git" && as_agent "gh api user --jq '\"   gh as: \" + .login'"
fi

step "herdr"
if ! as_agent "command -v herdr" >/dev/null; then
  as_agent "curl -fsSL https://herdr.dev/install.sh | sh"
fi
HERDR=$(as_agent "command -v herdr")
echo "   $HERDR ($(as_agent "herdr --version" 2>/dev/null || echo '?'))"

for u in herdr gh-bridge; do
  sed -e "s#@UID@#$AGENT_UID#g" -e "s#@HERDR@#$HERDR#g" "$APP/systemd/$u.service" > /etc/systemd/system/$u.service
done
systemctl daemon-reload
systemctl enable --now herdr.service
systemctl restart herdr.service   # picks up a changed env/unit
for _ in $(seq 30); do as_agent "herdr workspace list" >/dev/null 2>&1 && break; sleep 1; done

step "herdr-projects plugin (1.2.x; autonomous does not speak Shepherdr 2.0 yet)"
# Shepherdr 2.0 replaced herdr-projects with a new command (`shepherdr`) and data dir (`~/.shepherdr`).
# autonomous' bridge still speaks the 1.2.x CLI, so pin it. Override with HERDR_PROJECTS_REF in /etc/autonomous/env.
HERDR_PROJECTS_REF=${HERDR_PROJECTS_REF:-v1.2.27}
plugin_version() {
  as_agent "herdr plugin list --json" 2>/dev/null | jq -r '.result.plugins[]? | select(.plugin_id=="herdr-projects") | .version' | head -1
}
PLUGIN_VERSION=$(plugin_version)
if [ -z "$PLUGIN_VERSION" ]; then
  as_agent "herdr plugin install eliasstravik/herdr-projects --ref '$HERDR_PROJECTS_REF' --yes"
elif [ "${PLUGIN_VERSION%%.*}" != 1 ]; then
  echo "   !! herdr-projects $PLUGIN_VERSION is not 1.x; installing $HERDR_PROJECTS_REF"
  as_agent "herdr plugin install eliasstravik/shepherdr --ref '$HERDR_PROJECTS_REF' --yes"
fi
PLUGIN_ROOT=""
for _ in $(seq 30); do
  PLUGIN_ROOT=$(as_agent "herdr plugin list --json" 2>/dev/null | jq -r '.result.plugins[]? | select(.plugin_id=="herdr-projects") | .plugin_root' | head -1)
  [ -n "$PLUGIN_ROOT" ] && [ "$PLUGIN_ROOT" != null ] && break
  sleep 1
done
if [ -n "$PLUGIN_ROOT" ] && [ -f "$PLUGIN_ROOT/bin/run.sh" ]; then
  as_agent "cd '$PLUGIN_ROOT' && sh bin/run.sh install"   # writes the launcher ~/.herdr-projects/data/bin/herdr-projects
else
  echo "   !! herdr-projects plugin root not found, open the board once: herdr-projects"
  MISSING+=("herdr-projects plugin $HERDR_PROJECTS_REF")
fi
# No `herdr-projects update` here: it can pull 2.0. The plugin ref above is the version autonomous tests.
as_agent "herdr-projects version" 2>/dev/null | sed 's/^/   /' || true
# The board key and sidebar rows. The board's first-run screen offers the same blocks; on a server nobody sees it.
HERDR_CFG="$HOME_DIR/.config/herdr/config.toml"
if ! grep -q 'herdr-projects.open' "$HERDR_CFG" 2>/dev/null; then
  install -d -o "$USER_NAME" -g "$USER_NAME" "$(dirname "$HERDR_CFG")"
  cat >> "$HERDR_CFG" <<'TOML'

# >>> herdr-projects (autonomous install.sh)
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "herdr-projects.open"

[ui.sidebar.agents]
rows = [["state_icon", "machine", "workspace", { token = "tab", rules = [{ equals = "Agent", hide = true }, { starts_with = "Reviewer #", hide = true }] }], ["agent"], [{ token = "$summary", dim = true }, "$progress"]]

[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], [{ token = "$summary", dim = true }, "$progress"]]
# <<< herdr-projects
TOML
  chown "$USER_NAME:$USER_NAME" "$HERDR_CFG"
  as_agent "herdr server reload-config" || echo "   !! herdr rejected the config, see $HERDR_CFG"
fi

step "Repos, labels, board settings"
BRIDGE="/usr/local/bin/uv run --script $APP/bridge.py"
if [ -n "${GH_TOKEN:-}" ]; then
  as_agent "$BRIDGE sync-repos" || MISSING+=("repos: sync-repos failed, see above")
  as_agent "$BRIDGE apply-settings" || true
fi

step "autonomous command"
cat > /usr/local/bin/autonomous <<'EOF'
#!/bin/sh
# Short commands for the box. Run with sudo: sudo autonomous <doctor|status|logs|attach|restart|update>
case "${1:-help}" in
  logs)    exec sh -c 'tail -n 20 -F /var/lib/autonomous/notify.log 2>/dev/null & journalctl -u gh-bridge -u herdr -f' ;;  # hook + units: journalctl ANDs -t with -u, so show both
  restart) exec systemctl restart herdr gh-bridge ;;
  update)  exec bash /opt/autonomous/install.sh ;;
  attach)  exec sudo -iu agent herdr ;;
  doctor|status|once|sync-repos|apply-settings)
    exec sudo -u agent -H sh -c 'cd ~ && export PATH="$HOME/.herdr-projects/data/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" &&
      set -a && . /etc/autonomous/env && set +a &&
      exec /usr/local/bin/uv run --script /opt/autonomous/bridge.py "$@"' sh "$@" ;;
  *) echo "usage: sudo autonomous doctor|status|logs|attach|restart|update"; exit 2 ;;
esac
EOF
chmod 755 /usr/local/bin/autonomous

step "gh-bridge"
if [ ${#MISSING[@]} -eq 0 ]; then
  systemctl enable gh-bridge.service >/dev/null 2>&1
  systemctl restart gh-bridge.service
else
  systemctl disable --now gh-bridge.service >/dev/null 2>&1 || true
fi

step "Doctor"
/usr/local/bin/autonomous doctor || true

if [ ${#MISSING[@]} -gt 0 ]; then
  printf '\n\033[1;33mStill to do:\033[0m\n'; printf '  - %s\n' "${MISSING[@]}"
  echo "  then re-run: sudo bash $APP/install.sh"
fi
cat <<EOF

Next, once (pi's first run, the board asks for it):
  sudo -iu $USER_NAME
  cd ~/repos/<owner>/<repo> && pi      # check it starts, the model answers, then quit
  herdr                                 # attach; the board: ctrl+b a

Then:   sudo autonomous doctor | status | logs | attach | restart | update
EOF
