# Working on this repo

This repo is an installer + a small bridge, so "testing" means deploying to a real Linux box with systemd.
It also carries the kit it installs into other repos (`.github/`, `.herdr-projects.json`), so the first repo
the agent works on is autonomous itself: **self-hosting / dogfooding**.

## Commands

- Install: `make setup` — no dependencies; checks Python >= 3.11 and bash.
- Check (must pass before you finish): `make check` — shell/Python syntax, example config, secret scan, CHEATSHEET length.
  It is also the GitHub check (`.github/workflows/check.yml`); keep it offline and non-interactive.
- Run: not on your machine. The bridge runs on a VPS: `./deploy.sh <host>` (see below).
- Local e2e: `tests/docker.sh up` (privileged systemd container, no KVM), then `tests/docker.sh scenario work`.
  This is the fast loop on this host; `tests/docker.sh down` removes it.
- Before pushing (slower): `make secrets-history`.

## Map of the repo

| Path | What |
|---|---|
| `install.sh` | idempotent VPS installer: packages, user `agent`, herdr + plugin, pi, repos, labels, systemd |
| `deploy.sh` | laptop → `/opt/autonomous`, then runs `install.sh` there |
| `bridge.py` | gh-bridge (Python standard library only): GitHub issues ↔ herdr-projects board |
| `prompts/` | task/consult briefs and the messages that carry issue comments |
| `config/` | `*.example` files; the real ones live in `/etc/autonomous` on the server |
| `systemd/` | `herdr.service`, `gh-bridge.service` |
| `repo-kit/` | files applied to another repo + `apply.sh`; this repo uses filled-in copies at the root |
| `scripts/vm.sh` | throwaway Ubuntu VM on a Linux host (KVM or TCG) |
| `tests/` | sandbox test config, Docker container and calculator end-to-end scenarios |
| `tests/docker.sh` | privileged systemd container: native speed, no KVM |
| `tests/docker/` | Dockerfile for that systemd container |
| `.github/` | issue/PR templates, `check.yml`, `release.yml` |
| `.herdr-projects.json` | this repo's own setup/check for the board (self-hosting) |

## Test environment

Use a throwaway environment. A plain container won't do, but a **privileged systemd container** does: `loginctl`,
user managers and systemd units work with systemd as PID 1 and cgroup delegation. That is `tests/docker.sh`, the
fast local loop (no KVM). A real VM (Lima/QEMU) isolates better and is the release-grade check.
**Never test by deploying to the live host** (`./deploy.sh` there or `sudo autonomous update`): it restarts herdr
and gh-bridge and can kill your own session.

### Docker (fast, no KVM)

```bash
tests/docker.sh up                 # build + start + install (a few minutes)
tests/docker.sh scenario work      # issue -> PR -> review -> /approve -> merge -> release
tests/docker.sh logs               # gh-bridge + herdr
tests/docker.sh down               # remove the container and its token/keys
```

It uses `--privileged --cgroupns=host` and a writable `/sys/fs/cgroup`; the kernel is the host's, so do not use
production secrets in it. The test config is sandbox-only and `install.sh` pins herdr-projects `v1.2.27` (Shepherdr
2.0 renamed the CLI/data dir; override with `HERDR_PROJECTS_REF`). The same repo-kit and `make check` run inside.

### macOS (Lima)

```bash
brew install lima                                     # once
limactl start --name=autonomous --tty=false --cpus=4 --memory=4 --disk=20 --mount-none --containerd=none template:ubuntu-24.04
# Lima writes the SSH config to ~/.lima/autonomous/ssh.config
```

Deploy (needs `config/autonomous.toml` and `config/env`, both gitignored, copied from the `*.example` files):

```bash
SSH_CONFIG=~/.lima/autonomous/ssh.config ./deploy.sh lima-autonomous
ssh -F ~/.lima/autonomous/ssh.config lima-autonomous 'sudo autonomous doctor'
```

### Linux (Lima or raw QEMU)

Lima works on Linux too, with the same QEMU driver. Without `/dev/kvm` it falls back to TCG (slow, but it is a real
systemd VM). If the CPU has `svm`/`vmx`, enable virtualization in the BIOS first; a TCG-only host is fine for a
single full e2e run, not for every task. On a TCG-only host keep it small:

```bash
limactl start --name=autonomous --tty=false --cpus=2 --memory=3 --disk=20 --mount-none --containerd=none template:ubuntu-24.04
SSH_CONFIG=~/.lima/autonomous/ssh.config tests/deploy-vm.sh lima-autonomous
ssh -F ~/.lima/autonomous/ssh.config lima-autonomous 'sudo autonomous doctor'
```

`tests/deploy-vm.sh` deploys the test config with **only the two sandbox repos**, not the live config. Without
Lima, `scripts/vm.sh up`/`deploy` is a raw-QEMU fallback with the same cloud image and test config.

Docker Sandboxes (`sbx`) is not the same thing: local sandboxes need `/dev/kvm` (`sbx diagnose` says there is no
hypervisor), and cloud sandboxes are agent containers without systemd/`loginctl`. The plain privileged systemd
container in `tests/docker.sh` is what works here; `sbx` is for an agent, not for testing `install.sh`.

The calculator scenarios are automated in `tests/scenario.sh`; see [tests/README.md](tests/README.md).

### Both

- Secrets: the human puts them into `/etc/autonomous/env` on the VM. On Linux, `tests/deploy-vm.sh`,
  `scripts/vm.sh deploy` and `tests/docker.sh` copy the host's `/etc/autonomous/env` into the sandbox without
  printing it; never ask for secrets in chat, never print them.
- Before the first task: run `pi` once as `agent` in a repo (`sudo -iu agent`, `cd ~/repos/<owner>/<repo> && pi`).
- Quick iteration on `bridge.py` without a full deploy (Lima):
  `rsync -az -e "ssh -F ~/.lima/autonomous/ssh.config" --rsync-path="sudo rsync" bridge.py lima-autonomous:/opt/autonomous/ && ssh -F ~/.lima/autonomous/ssh.config lima-autonomous 'sudo systemctl restart gh-bridge'`
  Raw QEMU: use `~/.autonomous-vm/ssh.config` and host `autonomous-vm`.

### Delete

```bash
tests/docker.sh down              # Docker: removes the container, its token/keys and /var/lib/autonomous
limactl delete -f autonomous      # Lima: removes the VM, its disk (~3 GB) and the ssh config, including the GH token
rm -rf ~/.autonomous-vm           # raw QEMU: after `scripts/vm.sh down`
```

## What to test end-to-end

Use private sandbox repos, never real ones: a tiny project with a `Makefile` (`setup`, `check`) and the `repo-kit/` files.

| Scenario | Expect |
|---|---|
| issue + `start` | `agent:working` → draft PR → review → `agent:ready` + comment |
| agent asks (AC says "ask first") | `agent:needs-input`, an answer in the issue reaches it |
| comment on the PR | agent pushes a fix, second review round |
| issue + `consult`, then "write it up", then `start` | answers on the issue, body rewritten, consult ends, work task starts |
| `/approve` in the issue, or a manual merge | merged, issue closed, `agent:done`, release tag |
| two repos at once | both tasks run in parallel |
| reboot the VM / `docker restart` | `herdr`, `gh-bridge` and the herdr-projects keeper come back, board state kept |

Watch with `herdr-projects list --json` (as `agent`) and `sudo journalctl -u gh-bridge -f`.
`tests/scenario.sh` automates the calculator cases against the private sandbox repos; see [tests/README.md](tests/README.md).

## Definition of Done

1. Every acceptance criterion of the issue has a test, or a stated manual proof in the PR description.
2. `make check` passes locally (and in CI).
3. Installer/systemd/bridge/config changes are exercised in the Docker sandbox (`tests/docker.sh`) or a throwaway
   VM (Lima/`scripts/vm.sh`) when you can; otherwise the PR says they were not and gives the exact commands for the
   human. Never test them on the live host.
4. The PR description lists the acceptance criteria, ticked, each with how it was verified.
5. The diff stays inside the issue's scope. Anything else you noticed goes under "Also noticed" in the PR, not in code.
6. No secrets, no generated files, no unrelated formatting changes.
7. If the issue is unclear, ask instead of guessing:
   `herdr-projects report --state blocked --kind question --msg "<question>"`.

## Conventions

- `bridge.py` uses the Python standard library only; a new dependency needs a reason in the issue.
- Shell: `set -euo pipefail`, idempotent installers, quoted paths, no interactive prompts.
- Keep `CHEATSHEET.md` to one A4 page (~80 lines); `make check` enforces the limit.
- Do not loosen checks or merge settings to make a task pass. `.herdr-projects.json` is read from the base commit
  on purpose: code written in a task must not change the rules its own branch is checked by.

## Before pushing

- `make secrets-history` (no hits except placeholders such as `you@example.com` and `noreply`).
- `make check`.
- `CHEATSHEET.md` still fits one A4 page.
