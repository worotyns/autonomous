# autonomous

**GitHub issue z labelem `start` → agent na Twoim VPS pisze kod, testuje, otwiera PR, przechodzi review drugiego agenta i czeka na Twoje `/approve` → merge → tag i release.** Dla wielu repo naraz, z jednego serwera.

```
label "start" ──► gh-bridge ──► herdr-projects ──► pi w osobnym worktree ──► check ──► draft PR ──► reviewer ──► czeka
komentarz w issue ───────────► agent (pytania, doprecyzowania)                                     │
komentarz / review w PR ─────────────────────────► agent poprawia i pushuje                        │
/approve w issue ───────────────────────────────────────────────────────────────────────────────► merge ──► release.yml: tag vX.Y.Z → release → obraz w GHCR
```

Przetestowane end-to-end na Ubuntu 24.04 (Lima VM, arm64): 2 repo, zadania równolegle, pytanie agenta → odpowiedź w issue, poprawki z komentarza w PR, `/approve`, ręczny merge, restart maszyny.

## Z czego to jest zrobione

| Element | Rola |
|---|---|
| [herdr](https://herdr.dev) | serwer terminali dla agentów: sesje żyją w tle, można się podpiąć przez SSH i podejrzeć agenta |
| [herdr-projects](https://github.com/eliasstravik/herdr-projects) | plugin herdr: board zadań, multi-repo, worktree na zadanie, check, draft PR, review drugim agentem, merge dopiero po akceptacji, reakcja na komentarze w PR |
| [pi](https://github.com/earendil-works/pi) | agent kodujący (harness) |
| [OpenRouter](https://openrouter.ai) | modele, domyślnie `deepseek/deepseek-v4.1-flash` (tani; zmiana jedną linią w configu) |
| `gh` | GitHub CLI: issues, PR, labele |
| **gh-bridge** (`bridge.py`, ten repo) | spina GitHub issues z boardem: `start` → zadanie, komentarze w issue → agent, stan zadania → labele `agent:*`, `/approve`, `/stop` |
| **repo-kit** (ten repo) | do każdego repo: szablon issue z acceptance criteria, `AGENTS.md` (Definition of Done), `release.yml` |

## TL;DR: setup VPS

Wystarczy dowolny VPS z Ubuntu 22.04+ / Debian 12+ (x86_64 albo arm64), 2+ GB RAM i dostępem `root` albo usera z sudo.

```bash
git clone https://github.com/worotyns/autonomous && cd autonomous
cp config/autonomous.example.toml config/autonomous.toml   # wpisz swoje repo w [[repo]], [git] name/email
cp config/env.example config/env                           # OPENROUTER_API_KEY, GH_TOKEN
./deploy.sh root@<ip-vps>                                  # albo ubuntu@<ip> (sudo), opcjonalnie --tailscale
```

Potem raz na VPS (herdr-projects wymaga jednego ręcznego startu agenta):

```bash
sudo -iu agent
cd ~/repos/<owner>/<repo> && pi     # odpowiada? wyjdź (ctrl+c)
```

Do każdego repo dorzuć repo-kit (otwiera PR, niczego nie nadpisuje):

```bash
repo-kit/apply.sh owner/repo1 owner/repo2
```

W tym PR uzupełnij w `AGENTS.md` komendy (install, check) i mapę repo, a w `.herdr-projects.json` komendę checku. Słabszy model działa dużo lepiej, gdy nie musi zgadywać.

**Tokeny:**
- `GH_TOKEN` to token konta, którego komentarze mają sterować agentem (zwykle Twój). Najlepiej fine-grained, tylko na wybrane repo: Contents, Issues, Pull requests (RW), Actions, Commit statuses, Metadata (R).
- `OPENROUTER_API_KEY`: osobny klucz z limitem kredytów.

## Jak z tym pracować

| Chcę | Robię |
|---|---|
| zlecić zadanie | issue z szablonu **Agent task** (konkretne acceptance criteria!) + label `start` |
| agent ma pytanie | issue dostaje `agent:needs-input` i komentarz z pytaniem, odpowiadasz komentarzem |
| doprecyzować / zapytać | komentarz w issue (trafia do agenta po ~90 s ciszy) |
| poprawki w kodzie | komentarze / review na PR |
| gotowe do merge | `agent:ready` + komentarz z linkiem do PR → `/approve` w issue (albo merge ręcznie, też zadziała) |
| zatrzymać | `/stop` w issue |
| typ wersji | label na PR: `release:minor`, `release:major`, `release:none` (domyślnie patch) |
| podejrzeć agenta na żywo | `ssh` na VPS → `sudo -iu agent` → `herdr` → board: `ctrl+b a` (wyjście: `ctrl+b q`) |
| status mostu | `sudo -iu agent /usr/local/bin/uv run --script /opt/autonomous/bridge.py status` |
| logi | `journalctl -u gh-bridge -f`, `journalctl -u herdr -f` |
| dodać repo | dopisz `[[repo]]` w `/etc/autonomous/autonomous.toml` → `sudo bash /opt/autonomous/install.sh` |
| aktualizacja | `./deploy.sh <host>` jeszcze raz (instalator jest idempotentny, nie rusza configu w `/etc/autonomous`) |

Labele na issue: `agent:working` → (`agent:needs-input`) → `agent:ready` → `agent:done`, albo `agent:failed`.

## Test lokalnie zamiast VPS (macOS, Lima)

```bash
brew install lima
limactl start --name=autonomous --tty=false --cpus=4 --memory=4 --disk=20 --mount-none template:ubuntu-24.04
limactl show-ssh --format=config autonomous | sed 's/^Host .*/Host lima-autonomous/' > ~/.lima/autonomous/ssh.config.host
SSH_CONFIG=~/.lima/autonomous/ssh.config.host ./deploy.sh lima-autonomous
# sprzątanie: limactl delete -f autonomous
```

Zwykły kontener (Docker/Podman) się nie nada, bo instalator potrzebuje systemd.

## Co gdzie leży

| | |
|---|---|
| `deploy.sh` | z laptopa: rsync do `/opt/autonomous` + `install.sh` na serwerze |
| `install.sh` | idempotentny instalator: pakiety, user `agent`, herdr + plugin, pi, repo, labele, ustawienia boardu, usługi systemd |
| `bridge.py` | gh-bridge (Python, sama biblioteka standardowa): `run`, `once`, `status`, `doctor`, `sync-repos`, `apply-settings`, `notify` |
| `prompts/` | brief zadania z issue i wiadomość z komentarzem z issue |
| `config/` | przykładowy config i env; prawdziwe trafiają do `/etc/autonomous/` |
| `systemd/` | `herdr.service` (`herdr server`) i `gh-bridge.service` |
| `repo-kit/` | pliki do repo + `apply.sh` |
| [IDEA.md](IDEA.md), [PLAN.md](PLAN.md) | dlaczego tak, przegląd alternatyw, plan dalszych kroków |

Na VPS: config w `/etc/autonomous/`, stan mostu w `/var/lib/autonomous/`, repo w `/home/agent/repos/<owner>/<repo>`, worktree zadań w `/home/agent/.herdr/worktrees/`.

## Bezpieczeństwo, przeczytaj zanim podepniesz prawdziwe repo

- **Agent ma pełny shell usera `agent`** (pi nie pyta o zgodę). Nie trzymaj na tym VPS sekretów produkcyjnych i nie dawaj userowi `agent` sudo.
- **Agentem sterują tylko zaufani:** komentarze w PR budzą go tylko od właściciela `GH_TOKEN`, a w issue tylko od niego i od `allow_authors`. Label `start` dodany przez kogoś spoza listy jest zdejmowany. Komentarze obcych są ignorowane (prompt injection).
- **Nic nie merguje się bez Ciebie** (`merge.by = you`, `merge.checks = github`). Dodatkowo włącz branch protection z wymaganym CI.
- Ustaw limit kredytów na kluczu OpenRouter. Limit czasu na zadanie to `limit.time` w configu.

## Znane ograniczenia

- Labele po merge'u odświeżają się z pollingiem (do ~60 s).
- `release.yml` taguje każdy push do `main`, także commity spoza agentów (`release:none` na PR wyłącza).
- Przetestowane na jednym typie maszyny (Ubuntu 24.04 arm64 w Lima). Na x86 VPS powinno działać tak samo (instalatory herdr i herdr-projects wspierają oba), ale tego nie sprawdzałem.
