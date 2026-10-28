# PLAN — autonomous (herdr + herdr-projects + pi + gh-bridge)

> **Status (2026-10-28):** fazy 0–4 zrobione i przetestowane end-to-end na Lima VM (Ubuntu 24.04 arm64, 2 repo). Zostają: faza 5 (hardening) i pomysł trybu konsultacji (label `consult` → `herdr-projects add --chat`).

Zasada: każda faza kończy się działającym stanem, sprawdzonym na prawdziwym issue w 2+ repo (multi-repo od pierwszego dnia).

## Struktura repo `autonomous`

```
autonomous/
├── install.sh                  # one-command installer (idempotentny)
├── autonomous.example.toml     # lista repo, allowlista, modele, interwał
├── bridge.py                   # gh-bridge (uv run, PEP 723)
├── prompts/
│   ├── task_from_issue.md      # opis taska z issue + AC + DoD
│   └── issue_reply.md          # "odpowiedz w wątku przez gh issue comment"
├── repo-kit/                   # wrzucane do każdego repo
│   ├── .github/ISSUE_TEMPLATE/agent-task.yml
│   ├── .github/pull_request_template.md
│   ├── .github/workflows/release.yml   # push main → tag → build → deploy
│   ├── .herdr-projects.json    # setup + komenda checku
│   └── AGENTS.md               # Definition of Done
└── systemd/
    ├── herdr.service           # user unit (jeśli herdr nie daje własnego)
    └── gh-bridge.service
```

---

## Faza 0 — Spike (1 dzień)

Na VPS jako user `agent`, ręcznie:

- [ ] herdr (curl), pi (`npm i -g @mariozechner/pi-coding-agent`), gh (`gh auth login`, **konto, którego komentarze mają sterować agentem**), `OPENROUTER_API_KEY`.
- [ ] `herdr plugin install eliasstravik/herdr-projects` → `herdr-projects doctor`.
- [ ] Model: `pi --list-models | grep deepseek` → `herdr-projects set worker.agent pi/openrouter/deepseek/deepseek-v4.1-flash`, reviewer na mocniejszy model (`reviewer.agent`).
- [ ] Dwa repo sklonowane, task z CLI bez Coordinatora:
  ```bash
  herdr-projects add "Fix X (#12)" --repo api --merge.closes 12 --delegate
  ```
  Czy przejdzie pełny flow: worktree → pi → check → draft PR → review → czeka na „tak”?
- [ ] Komentarz na PR z Twojego konta → czy agent się budzi i pushuje poprawkę?
- [ ] `herdr-projects list` / `show`: format (JSON?) i jak z taska dostać pane (do odpowiedzi w issue).
- [ ] Keeper po zamknięciu boardu, po wylogowaniu z SSH i po reboocie.
- [ ] Web UI przez tailscale (approve z telefonu).
- [ ] Zmierzyć koszt 2–3 tasków w OpenRouter (worker + reviewer + Watcher).

**Wynik:** `NOTES.md` z działającymi komendami i ustawieniami.

## Faza 1 — Repo-kit (0.5 dnia)

- [ ] `agent-task.yml`: Kontekst, **Acceptance criteria** (`- [ ]`, wymagane), Out of scope, Jak testować.
- [ ] `AGENTS.md`: Definition of Done (każde AC → test / dowód, check zielony, PR z odhaczonymi AC, mały diff, przy niejasności `herdr-projects report <id> --state blocked --kind question --msg …`). Plus konkretne komendy i mapa repo dla słabszego modelu.
- [ ] `.herdr-projects.json`: setup (`npm ci` / `uv sync`) + komenda checku (`make check`).
- [ ] `release.yml`:
  ```yaml
  on: { push: { branches: [main] } }
  jobs:
    release:
      # 1. wersja: ostatni tag v* + bump (patch domyślnie; label PR release:minor/major albo conventional commits)
      # 2. git tag + push (GITHUB_TOKEN wystarczy, bo build jest w TYM SAMYM workflow)
      # 3. build + push obrazu, deploy (zależnie od repo; reusable workflow w org)
  ```
  Alternatywa: release-please (release PR do ręcznego merge).
- [ ] Skrypt `repo-kit/apply.sh <repo>`, który otwiera PR z repo-kitem w danym repo.

## Faza 2 — gh-bridge: label `start` → task (1–2 dni)

`bridge.py`, pętla co 60 s po wszystkich repo z `autonomous.toml`:

- [ ] Issue z labelem `start`, którego nie ma w SQLite. Label dodał ktoś z allowlisty (`issues/{n}/events`).
- [ ] `herdr-projects add "<tytuł> (#n)" --repo <r> --merge.closes <n> --delegate`, opis = `prompts/task_from_issue.md` (treść issue + AC + link). Zapis `repo, issue → task_id`.
- [ ] Label `start` → `agent:working` + komentarz w issue „🤖 task <id>”.
- [ ] Synchronizacja stanu boardu do labeli (co tick `herdr-projects list`):
  - needs you → `agent:needs-input` + komentarz z pytaniem agenta,
  - ready to approve → `agent:ready` + link do PR,
  - done → `agent:released` (albo zdejmij labele).
- [ ] CLI: `bridge.py run|once|status`.

**DoD fazy:** label `start` w dowolnym z N repo → draft PR po review, widoczny na boardzie i w GitHubie.

## Faza 3 — gh-bridge: komentarze w issue (1 dzień)

- [ ] `issues/{n}/comments?since=` dla issue z aktywnym taskiem. Autor z allowlisty, nie board/bot.
- [ ] Task `needs you` (agent zadał pytanie) → odpowiedź trafia jako odpowiedź do taska (sprawdzić w spike'u: komenda herdr-projects albo `herdr agent prompt`).
- [ ] Task pracuje → `issue_reply.md` → `herdr agent prompt <pane>` („odpowiedz w issue przez `gh issue comment`; zmień kod tylko, jeśli o to proszą”).
- [ ] Brak aktywnego taska → ignoruj.
- [ ] Kursory przesuwane po dostarczeniu (at-least-once), batch w oknie ciszy ~2 min.

(Komentarze w PR obsługuje herdr-projects. Nie dublujemy.)

## Faza 4 — Installer (1 dzień)

`install.sh` (Ubuntu/Debian, idempotentny):
- [ ] user `agent` + `loginctl enable-linger agent`
- [ ] git, gh, uv, node, herdr, pi, `herdr plugin install eliasstravik/herdr-projects`, opcjonalnie `persiyanov/herdr-reviewr`, tailscale
- [ ] klon wszystkich repo z `autonomous.toml` + rejestracja w herdr-projects
- [ ] ustawienia herdr-projects (`worker.agent`, `reviewer.agent`, `merge.by you`, `merge.checks github`, limity)
- [ ] `/etc/autonomous/env` (`OPENROUTER_API_KEY`, `GH_TOKEN`) `chmod 600`
- [ ] systemd: herdr + keeper (wg spike'u) + `gh-bridge.service` (`Restart=always`)
- [ ] na koniec `herdr-projects doctor` + checklista brakujących sekretów

```bash
ssh root@vps 'bash -s' < install.sh
```

Nowe repo = dopisz do `autonomous.toml` + `install.sh` ponownie (idempotentny) + `repo-kit/apply.sh`.

## Faza 5 — Hardening

- [ ] Izolacja: `herdr-devcontainer` albo `sbx` (jeśli `/dev/kvm`), bo agenci domyślnie mają pełne uprawnienia.
- [ ] Limity w herdr-projects (czas/tokeny per task) + limit kredytów na kluczu OpenRouter.
- [ ] Powiadomienia przy `needs you` / `ready to approve` (herdr notifications / Telegram z mostu).
- [ ] Metryki: % tasków bez rund poprawek, koszt per task, czas do PR. Na tej podstawie decyzja o modelach.
- [ ] Upstream do herdr-projects, jeśli się sprawdzi: import issue z labela, `--json` dla `list`.

## Plan B (jeśli herdr-projects nie siądzie w spike'u)

Własny dispatcher bezpośrednio na herdr CLI (`workspace create`, `pane run pi`, `agent prompt`, `agent wait`) + worktrunk (`wt`) do worktree z hookami + reviewer jako drugi pane `pi -p`. Zakres ~1,5–2 tygodnie. Wzorce do podpatrzenia: herdr-desk (watch-skrypty NDJSON z dedupe), odysseus (review gate, followup z PR).

---

## Szkic `autonomous.example.toml`

```toml
poll_interval = 60
allow_authors = ["your-login"]        # spójne z tym, czyje komentarze budzą agenta w PR
start_label = "start"

[models]
worker   = "pi/openrouter/deepseek/deepseek-v4.1-flash"
reviewer = "pi/openrouter/<mocniejszy model>"

[[repo]]
name = "acme/api"
[[repo]]
name = "acme/web"
# … kilkanaście
```

## Kolejność i szacunek

| Faza | Czas | Wynik |
|---|---|---|
| 0 spike | 1 d | herdr-projects + pi + OpenRouter działa na 2 repo |
| 1 repo-kit | 0.5 d | AC, DoD, release workflow |
| 2 bridge: start | 1–2 d | **label → PR w każdym repo** |
| 3 bridge: issue comments | 1 d | pełna pętla rozmowy |
| 4 installer | 1 d | one-command setup |
| 5 hardening | ciągle | — |

**MVP: ~4–5 dni po godzinach.** Lifecycle, review, PR feedback i merge niesie herdr-projects.
