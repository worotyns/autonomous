# Working on this repo

This repo is an installer + a small bridge, so "testing" means deploying to a real Linux box with systemd.
Use a throwaway Lima VM on macOS. Docker/Podman containers won't work, because the installer needs systemd and `loginctl`.

## Test VM: start

```bash
brew install lima                                     # once
limactl start --name=autonomous --tty=false --cpus=4 --memory=4 --disk=20 --mount-none template:ubuntu-24.04
limactl show-ssh --format=config autonomous | sed 's/^Host .*/Host lima-autonomous/' > ~/.lima/autonomous/ssh.config.host
```

Deploy (needs `config/autonomous.toml` and `config/env`, both gitignored, copied from the `*.example` files):

```bash
SSH_CONFIG=~/.lima/autonomous/ssh.config.host ./deploy.sh lima-autonomous
ssh -F ~/.lima/autonomous/ssh.config.host lima-autonomous 'sudo autonomous doctor'
```

- Secrets: the human puts them into `/etc/autonomous/env` on the VM themselves (`limactl shell autonomous`,
  `sudo nano /etc/autonomous/env`), then re-runs `sudo autonomous update`. Never ask for them in chat, never print them.
- Before the first task: run `pi` once as `agent` in a repo (`sudo -iu agent`, `cd ~/repos/<owner>/<repo> && pi`).
- Quick iteration on `bridge.py` without a full deploy:
  `rsync -az -e "ssh -F ~/.lima/autonomous/ssh.config.host" --rsync-path="sudo rsync" bridge.py lima-autonomous:/opt/autonomous/ && ssh -F ~/.lima/autonomous/ssh.config.host lima-autonomous 'sudo systemctl restart gh-bridge'`

## Test VM: delete

```bash
limactl delete -f autonomous      # removes the VM, its disk (~3 GB) and the ssh config, including the GH token on it
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
| `sudo reboot` | `herdr`, `gh-bridge` and the herdr-projects keeper come back, board state kept |

Watch with `herdr-projects list --json` (as `agent`) and `sudo journalctl -u gh-bridge -f`.

## Before pushing

- No secrets or personal data:
  `git grep -n -I -i -E "ghp_|github_pat_|sk-or-[a-z0-9]|BEGIN .*PRIVATE KEY|@[a-z0-9-]+\.(com|io|pl)" $(git rev-list --all) -- .`
  (the only hits should be placeholders like `you@example.com` and `noreply`).
- `bash -n install.sh deploy.sh repo-kit/apply.sh` and `python3 -c "import ast; ast.parse(open('bridge.py').read())"`.
- `CHEATSHEET.md` must still fit one A4 page.
