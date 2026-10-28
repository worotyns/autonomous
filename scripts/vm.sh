#!/usr/bin/env bash
# A throwaway Ubuntu 24.04 VM for testing the installer on a Linux host.
#
# KVM is used when /dev/kvm is usable; otherwise QEMU falls back to TCG (slow but works).
# Everything lives in ~/.autonomous-vm (override AUTONOMOUS_VM_DIR).
#
#   scripts/vm.sh up        # download the cloud image if needed, create/start the VM, wait for SSH
#   scripts/vm.sh down      # power off
#   scripts/vm.sh reset     # down + throw the disk away + up (fresh Ubuntu)
#   scripts/vm.sh deploy    # deploy this checkout with the sandbox test config and secrets
#   scripts/vm.sh config    # install only the test config/secrets into a running VM, then `autonomous update`
#   scripts/vm.sh ssh [cmd] # SSH into it
#   scripts/vm.sh status    # is it running, where is SSH, KVM or TCG?
#   scripts/vm.sh logs      # journalctl -u gh-bridge -u herdr -f
#   scripts/vm.sh console   # tail the serial console
#
# Root setup on Ubuntu/Debian (once):
#   sudo apt-get update && sudo apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils genisoimage
# If /dev/kvm exists but agent cannot read it:
#   sudo usermod -aG kvm agent      # new logins only; this script retries through `sg kvm`
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR=${AUTONOMOUS_VM_DIR:-$HOME/.autonomous-vm}
BASE=$DIR/ubuntu-24.04-minimal-cloudimg-amd64.img
SUMS=$DIR/SHA256SUMS
DISK=$DIR/autonomous-vm.qcow2
SEED=$DIR/seed.iso
USER_DATA=$DIR/user-data
META_DATA=$DIR/meta-data
SSH_KEY=$DIR/id_ed25519
SSH_CONFIG=$DIR/ssh.config
KNOWN_HOSTS=$DIR/known_hosts
PIDFILE=$DIR/qemu.pid
CONSOLE=$DIR/console.log
MONITOR=$DIR/monitor.sock

IMAGE_URL=${VM_IMAGE_URL:-https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img}
SUMS_URL=${VM_SUMS_URL:-https://cloud-images.ubuntu.com/minimal/releases/noble/release/SHA256SUMS}
VM_NAME=${VM_NAME:-autonomous-vm}
VM_PORT=${VM_PORT:-2222}
VM_CPUS=${VM_CPUS:-2}
VM_MEM=${VM_MEM:-3072}
VM_DISK=${VM_DISK:-20G}
SANDBOX_REPO_A=${SANDBOX_REPO_A:-worotyns/autonomous-sandbox}
SANDBOX_REPO_B=${SANDBOX_REPO_B:-worotyns/autonomous-sandbox-2}
HOST_ENV=${AUTONOMOUS_HOST_ENV:-/etc/autonomous/env}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf 'vm.sh: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing $1 (see the root setup in the header)"; }

# A group change does not reach the herdr session; re-run through sg for this one command.
if [ -e /dev/kvm ] && [ ! -r /dev/kvm ] && id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx kvm; then
  exec sg kvm -c "$(printf '%q ' "$0" "$@")"
fi

ssh_vm() { ssh -F "$SSH_CONFIG" -o ConnectTimeout=3 -o BatchMode=yes "$VM_NAME" "$@"; }
kvm_ok() { [ -r /dev/kvm ] && [ -w /dev/kvm ]; }
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

download_base() {
  install -d -m 700 "$DIR"
  if [ ! -f "$BASE" ]; then
    log "downloading $IMAGE_URL"
    curl -fsSL --retry 3 -o "$BASE.part" "$IMAGE_URL"
    mv "$BASE.part" "$BASE"
  fi
  if [ ! -f "$SUMS" ]; then curl -fsSL --retry 3 -o "$SUMS" "$SUMS_URL"; fi
  ( cd "$DIR" && grep " \*$(basename "$BASE")\$" "$SUMS" | sha256sum -c - >/dev/null ) \
    || die "checksum mismatch for $BASE; delete it and retry"
}

make_seed() {
  [ -f "$SSH_KEY" ] || ssh-keygen -q -t ed25519 -N '' -C "$VM_NAME" -f "$SSH_KEY"
  local pub; pub=$(cat "$SSH_KEY.pub")
  cat > "$USER_DATA" <<EOF
#cloud-config
hostname: $VM_NAME
manage_etc_hosts: true
users:
  - name: ubuntu
    groups: [adm, sudo]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    ssh_authorized_keys:
      - $pub
ssh_pwauth: false
disable_root: true
runcmd:
  - [ sh, -c, "touch /var/tmp/cloud-init-done" ]
EOF
  cat > "$META_DATA" <<EOF
instance-id: $VM_NAME-$(date +%s)
local-hostname: $VM_NAME
EOF
  cloud-localds "$SEED" "$USER_DATA" "$META_DATA"
}

make_ssh_config() {
  cat > "$SSH_CONFIG" <<EOF
Host $VM_NAME
  HostName 127.0.0.1
  Port $VM_PORT
  User ubuntu
  IdentityFile $SSH_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
  UserKnownHostsFile $KNOWN_HOSTS
  ServerAliveInterval 15
EOF
  chmod 600 "$SSH_CONFIG" "$SSH_KEY"
}

start_vm() {
  need qemu-system-x86_64; need qemu-img; need cloud-localds; need ssh-keygen
  if running; then log "$VM_NAME is already running (pid $(cat "$PIDFILE"))"; return 0; fi
  download_base; make_seed; make_ssh_config
  install -d -m 700 "$DIR"
  if [ ! -f "$DISK" ]; then
    log "creating $VM_DISK disk from the cloud image"
    qemu-img create -q -f qcow2 -F qcow2 -b "$BASE" "$DISK" "$VM_DISK"
  fi
  rm -f "$PIDFILE" "$MONITOR"
  local accel
  if kvm_ok; then
    accel=(-accel kvm -cpu host)
    log "using KVM"
  else
    # Nehalem is the x86-64-v2 baseline: enough for Node/pi, cheaper to emulate than `max`.
    accel=(-accel tcg,thread=multi -cpu Nehalem)
    log "no usable /dev/kvm; using QEMU TCG (slow, expect minutes where KVM needs seconds)"
  fi
  log "starting $VM_NAME (${VM_CPUS} vCPU, ${VM_MEM} MB, ssh 127.0.0.1:$VM_PORT)"
  qemu-system-x86_64 \
    -name "$VM_NAME" -machine q35 "${accel[@]}" -smp "$VM_CPUS" -m "$VM_MEM" \
    -drive "file=$DISK,if=virtio,format=qcow2" \
    -drive "file=$SEED,if=virtio,format=raw,readonly=on" \
    -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:${VM_PORT}-:22" \
    -display none -serial "file:$CONSOLE" \
    -monitor "unix:$MONITOR,server,nowait" \
    -pidfile "$PIDFILE" -daemonize
  log "booting; waiting for SSH (console: $CONSOLE)"
  local i
  for i in $(seq 1 300); do
    if ssh_vm true >/dev/null 2>&1; then
      log "SSH is up; waiting for cloud-init"
      ssh_vm 'cloud-init status --wait >/dev/null 2>&1 || true' || true
      log "VM ready: ssh -F $SSH_CONFIG $VM_NAME"
      return 0
    fi
    if ! running; then tail -20 "$CONSOLE" >&2 || true; die "qemu exited during boot"; fi
    sleep 2
  done
  tail -20 "$CONSOLE" >&2 || true
  die "SSH did not come up after 10 minutes"
}

stop_vm() {
  if ! running; then rm -f "$PIDFILE"; log "$VM_NAME is not running"; return 0; fi
  local pid; pid=$(cat "$PIDFILE")
  log "powering off $VM_NAME"
  ssh_vm 'sudo poweroff' >/dev/null 2>&1 || true
  local i
  for i in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; sleep 2; fi
  if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null || true; fi
  rm -f "$PIDFILE"
}

reset_vm() {
  stop_vm
  rm -f "$DISK" "$SEED" "$KNOWN_HOSTS" "$CONSOLE" "$MONITOR" "$USER_DATA" "$META_DATA"
  start_vm
}

make_test_config() {
  local out=$1
  install -d -m 700 "$out/config"
  [ -n "$SANDBOX_REPO_A" ] && [ -n "$SANDBOX_REPO_B" ] || die "SANDBOX_REPO_A/B must be set"
  sed -e "s|{{REPO_A}}|$SANDBOX_REPO_A|g" -e "s|{{REPO_B}}|$SANDBOX_REPO_B|g" \
    "$ROOT/tests/autonomous.test.toml.in" > "$out/config/autonomous.toml"
  if [ -r "$HOST_ENV" ]; then
    cp "$HOST_ENV" "$out/config/env"
  elif [ -r "$ROOT/config/env" ]; then
    cp "$ROOT/config/env" "$out/config/env"
  else
    die "cannot read $HOST_ENV (or config/env); secrets are needed for the VM"
  fi
  chmod 600 "$out/config/env"
}

deploy_vm() {
  start_vm >/dev/null
  local tmp; tmp=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" EXIT
  tar -C "$ROOT" --exclude .git --exclude config/env --exclude config/autonomous.toml -cf - . | tar -C "$tmp" -xf -
  make_test_config "$tmp"
  log "deploying $ROOT to $VM_NAME (sandbox: $SANDBOX_REPO_A, $SANDBOX_REPO_B)"
  ( cd "$tmp" && SSH_CONFIG="$SSH_CONFIG" ./deploy.sh "$VM_NAME" "$@" )
  log "VM deployed; try: scripts/vm.sh ssh 'sudo autonomous doctor'"
}

config_vm() {
  start_vm >/dev/null
  local tmp; tmp=$(mktemp -d)
  trap "rm -rf '$tmp'" EXIT
  make_test_config "$tmp"
  ssh_vm 'rm -rf /tmp/autonomous-test && mkdir -p /tmp/autonomous-test'
  scp -q -F "$SSH_CONFIG" "$tmp/config/autonomous.toml" "$tmp/config/env" "$VM_NAME:/tmp/autonomous-test/"
  ssh_vm 'sudo install -d -m 750 -o root -g agent /etc/autonomous && \
    sudo install -m 640 -o root -g agent /tmp/autonomous-test/autonomous.toml /etc/autonomous/autonomous.toml && \
    sudo install -m 640 -o root -g agent /tmp/autonomous-test/env /etc/autonomous/env && \
    rm -rf /tmp/autonomous-test && sudo autonomous update'
}

status_vm() {
  if running; then
    printf '%s: running (pid %s)\n' "$VM_NAME" "$(cat "$PIDFILE")"
    printf 'ssh: ssh -F %s %s\n' "$SSH_CONFIG" "$VM_NAME"
    if kvm_ok; then echo "accel: kvm"; else echo "accel: tcg (no usable /dev/kvm)"; fi
    ssh_vm 'cloud-init status --long 2>/dev/null | head -5; echo; uptime' || true
  else
    echo "$VM_NAME: stopped"
  fi
}

case "${1:-help}" in
  up) start_vm ;;
  down) stop_vm ;;
  reset) reset_vm ;;
  deploy) shift; deploy_vm "$@" ;;
  config) config_vm ;;
  ssh) shift; exec ssh -F "$SSH_CONFIG" "$VM_NAME" "$@" ;;
  status) status_vm ;;
  logs) exec ssh -F "$SSH_CONFIG" -t "$VM_NAME" 'sudo journalctl -u gh-bridge -u herdr -f' ;;
  console) exec tail -f "$CONSOLE" ;;
  help|--help|-h) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
  *) die "usage: $0 up|down|reset|deploy|config|ssh [cmd]|status|logs|console" ;;
esac
