# End-to-end tests on a Linux host

The project is tested against **private sandbox repos**, never real projects. On a Linux host use Lima when
it is installed, or the raw-QEMU fallback `scripts/vm.sh` when it is not. On macOS the Lima path in
[AGENTS.md](../AGENTS.md) still applies.

> Docker Sandboxes (`sbx`) is **not** a replacement here. `sbx diagnose` reports no hypervisor on this host
> (`/dev/kvm` missing), so local sandboxes cannot start. Cloud sandboxes (`sbx --cloud`) are agent containers,
> not a systemd VM, and the installer needs systemd + `loginctl`. Use `sbx` to run an agent in isolation, not
> to test `install.sh`.

## 1. VM with Lima

On Linux, Lima's QEMU driver works without root and falls back to TCG when `/dev/kvm` is missing (slow, but
it is a real systemd VM).

**KVM first.** If `/dev/kvm` is missing but the CPU has `svm`/`vmx`, virtualization is usually disabled in the
BIOS. On the HP EliteDesk here: F10 at boot → Advanced → System Options / CPU Configuration → **SVM Mode**
(AMD-V) → Enabled. Then, as root:

```bash
sudo modprobe kvm_amd          # kvm_intel on Intel
ls -l /dev/kvm                 # should be root:kvm, 660
sudo usermod -aG kvm agent     # log out/in, or use `sg kvm` for one command
```

If `modprobe` says `Operation not supported`, `sudo dmesg | grep -i -E 'kvm|svm'` usually says that the BIOS
disabled SVM. Without KVM, expect a first boot of several minutes and a long `apt`/`npm` install; that is why
the scenario timeouts are 40 minutes.

```bash
limactl start --name=autonomous --tty=false --cpus=2 --memory=3 --disk=20 \
  --mount-none --containerd=none template:ubuntu-24.04
# Lima writes the SSH config to ~/.lima/autonomous/ssh.config
```

With KVM, use `--cpus=4 --memory=4` like on macOS. On a TCG-only host keep the VM small: the host has to
run QEMU and the live `gh-bridge` at the same time. `--containerd=none` skips Lima's container provisioning;
autonomous does not need it.

## 2. Deploy the test config

`tests/deploy-vm.sh` copies this checkout to an already-running VM and generates a config that contains
**only the two sandbox repos**. It takes the secrets from `/etc/autonomous/env` (or `config/env`) without
printing them; the copy lives in the VM's `/etc/autonomous` and in a temporary directory that is removed
afterwards. It refuses to target the live host.

Lima:

```bash
SSH_CONFIG=~/.lima/autonomous/ssh.config tests/deploy-vm.sh lima-autonomous
ssh -F ~/.lima/autonomous/ssh.config lima-autonomous 'sudo autonomous doctor'
```

The test config is `tests/autonomous.test.toml.in`: 20 s poll/quiet window, sandbox repos only,
`limit.time = 30m`. Change the repos with `SANDBOX_REPO_A` / `SANDBOX_REPO_B`:

```bash
SANDBOX_REPO_A=owner/calc-a SANDBOX_REPO_B=owner/calc-b \
  SSH_CONFIG=~/.lima/autonomous/ssh.config tests/deploy-vm.sh lima-autonomous
```

## 3. VM without Lima (raw QEMU)

Root packages, once (Ubuntu/Debian):

```bash
sudo apt-get update
sudo apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils genisoimage
```

`scripts/vm.sh` uses the same Ubuntu cloud image directly. KVM is used when `/dev/kvm` is usable; otherwise
it falls back to QEMU TCG. If `/dev/kvm` exists but `agent` cannot read it, the admin adds the group and the
script retries through `sg kvm`:

```bash
sudo usermod -aG kvm agent
scripts/vm.sh up          # first run downloads ~250 MB and boots Ubuntu 24.04
scripts/vm.sh status
scripts/vm.sh ssh         # ubuntu@127.0.0.1:2222, key in ~/.autonomous-vm
```

State lives in `~/.autonomous-vm` (override with `AUTONOMOUS_VM_DIR`). `down` powers off, `reset` throws
the disk away and boots a fresh Ubuntu, `console` tails the serial console when SSH is not there yet.

```bash
scripts/vm.sh deploy      # this checkout + the sandbox test config
scripts/vm.sh ssh 'sudo autonomous doctor'
scripts/vm.sh logs
```

After changing only the config or secrets, `scripts/vm.sh config` installs it again and runs
`sudo autonomous update`.

## 4. Sandbox repos

Two private repos hold the calculator:

| Repo | Base tag | Base contents |
|---|---|---|
| `worotyns/autonomous-sandbox` | `test-base` | `add`, `sub` + unittest tests, `Makefile`, `AGENTS.md`, `.herdr-projects.json`, release workflow |
| `worotyns/autonomous-sandbox-2` | `test-base` | same (a second repo for the parallel scenario) |

The base tag points at the initial calculator commit. `tests/scenario.sh reset` closes stale test issues and
PRs, deletes task branches, and force-moves `main` back to `test-base`, so every run starts from the same
code. Releases/tags from earlier runs are left alone; the next merge bumps the version again.

## 5. Scenarios

The script talks to GitHub only; the VM's gh-bridge picks up the issues and labels. Run it from the repo
root, with `gh` authenticated as the account in `GH_TOKEN`:

```bash
tests/scenario.sh work        # start -> draft PR -> review -> agent:ready -> /approve -> merge + release
tests/scenario.sh consult     # consult -> agent answer -> "write it up" -> start -> PR -> merge + release
tests/scenario.sh pr-comment  # ready PR -> comment on the PR -> new commit -> ready -> /approve
tests/scenario.sh parallel    # two repos at once; both tasks run in parallel
tests/scenario.sh all         # reset, then all of the above in order
tests/scenario.sh reset       # reset only
```

Each command resets the sandbox repos first. `all` resets once and then runs work → consult → parallel →
pr-comment, so every later scenario builds on the code the earlier ones merged.

Timeouts are generous because TCG is slow: `READY_TIMEOUT=2400` (40 min), `MERGE_TIMEOUT=900`,
`RELEASE_TIMEOUT=900`, `POLL=15` seconds. Override them in the environment.

## 6. What the script checks

| Scenario | Checks |
|---|---|
| `work` | `agent:working` → `agent:ready` → `/approve` → `agent:done`, issue closed, new release, `calc.py` on `main` has `mul`/`div` |
| `consult` | `agent:consulting`, an agent comment, issue body rewritten with `## Acceptance criteria`, then a normal work task, merge, `history_calc.py` on `main` |
| `pr-comment` | a PR comment creates a new commit, the task returns to `agent:ready`, merge, `mod` on `main` |
| `parallel` | both issues are `agent:working` at the same time, both PRs become ready, both merge |

While it runs, watch the VM:

```bash
# Lima
ssh -F ~/.lima/autonomous/ssh.config lima-autonomous 'sudo autonomous status'
ssh -F ~/.lima/autonomous/ssh.config lima-autonomous 'sudo -iu agent herdr-projects list --json'

# raw QEMU
scripts/vm.sh logs
scripts/vm.sh ssh 'sudo autonomous status'
scripts/vm.sh ssh 'sudo -iu agent herdr-projects list --json'
```

## 7. Safety

- Only private sandbox repos are in the VM config. The script never creates issues in `worotyns/autonomous`.
- The VM gets the same `GH_TOKEN` and `OPENROUTER_API_KEY` as the live host; treat the VM as trusted and
  delete it when it is not needed.
- The VM's agent has a full shell inside the VM only. Do not put production secrets in the sandbox repos.
