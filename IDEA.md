# autonomous — GitHub-driven coding agents na herdr (multi-repo)

## Executive summary

**Wymagania:** multi-repo by default (kilkanaście repo na jednym VPS) · herdr jako runtime · Pi jako harness · OpenRouter, default `deepseek/deepseek-v4.1-flash` · GitHub jako interfejs (issue → PR → review → merge → tag).

**Rekomendacja: [herdr-projects](https://github.com/eliasstravik/herdr-projects) (plugin herdr, MIT, ~900★, bardzo aktywny) + mały most GitHub → board (~150 linii) + release w CI repo.**

herdr-projects to „mission control” dla herdr. **Jeden board na maszynę obsługuje wszystkie repo** (task otagowany repo, `data/tasks/<repo>/<n>/`). Z pudełka:

- task → **własny worktree + branch `work/<n>-…` + własny space w herdr** → agent (Claude Code, Codex, **pi**, cokolwiek wspiera herdr),
- „finish line”: commit → merge main → check repo → **draft PR** → **drugi agent robi review** → porażka wraca do agenta,
- **merge tylko po Twoim „tak”**, dokładnie na zreviewowanym commicie (`merge.by you` domyślnie), opcjonalnie czeka na zielone checki GitHuba (`merge.checks github`),
- **komentarze w PR budzą agenta**. Zweryfikowane w kodzie: `crates/flow/src/housekeeping/prs.rs` reaguje na komentarze, review i failed checks. Liczą się tylko komentarze zalogowanego usera `gh`, nie innych ludzi ani botów, co jest przy okazji ochroną przed prompt injection,
- `--merge.closes <nr>`: `Closes #n` w PR, issue zamyka się przy merge,
- limity czasu i tokenów, fallback agentów, kolejka, `--after` (łańcuchy tasków między repo),
- **keeper** działa przy zamkniętym boardzie, czyli nadaje się na VPS; jest web UI na telefon przez tailnet,
- per-agent model: `--worker.agent pi/<provider>/<model>`, osobno dla reviewera.

**Czego brakuje i co dopisujemy:**

| Potrzeba | Jak |
|---|---|
| Issue z labelem `start` → task | **most** (Python, systemd, `gh api` co 60 s) → `herdr-projects add "<tytuł>" --repo <r> --merge.closes <n> --delegate`. herdr-projects celowo nie ma webhooków, a ich „automations” to prompt dla LLM. Most jest deterministyczny i tańszy. |
| Komentarz w issue → agent odpisuje | **most**: do agenta taska przez `herdr agent prompt` / `pane send-text` (pane z `herdr-projects list`). Do zweryfikowania. |
| Merge → tag → build/deploy | **workflow w repo**: `on: push: branches: [main]` → wylicz wersję → tag → build/deploy **w tym samym workflow** (tag zrobiony `GITHUB_TOKEN`-em nie odpala innych workflowów). Albo release-please. Zero LLM. |
| Acceptance criteria | szablon issue (wymagane AC) + `AGENTS.md` (Definition of Done) + `.herdr-projects.json` w repo (komenda checku) |

### Przegląd tego, co znalazłem w ekosystemie herdr

| Plugin | ★ | Ocena |
|---|---|---|
| **eliasstravik/herdr-projects** | ~900, MIT | **wybór**: multi-repo, pi, reviewer, PR feedback, merge na „tak”, keeper |
| duyet/herdr-desk | 1, brak licencji | multi-repo + watch-skrypty (dobry wzorzec eventów), ale „manager-agent” sam decyduje, co robić, i **auto-merguje**. Brak licencji = nie bierzemy |
| jpolec/herdr-plugin-odysseus | 9, MIT | `tracker import --label agent-ready`, review gate, draft PR, followup z komentarzy PR. Ciekawy, ale mały i od miesiąca bez commitów |
| steig/worktender | — | tylko Claude Code |
| mkdir700/herdr-plugin-worktree, ogulcancelik/herdr-plugin-github-start | — | issue → worktree → agent, ale ręcznie z popupu |
| DnzzL/herdr-automations, mrcndz/herdr-routines | — | cron → prompt w świeżym worktree, bez GitHub eventów |
| Matovidlo/herdr-pr-tracker, wyattjoh/herdr-plugin-gh-pr | — | tylko podgląd stanu PR |
| persiyanov/herdr-reviewr, jhochenbaum/herdr-hunk-diff | ~950 / ~150 | ręczne review diffu z odesłaniem komentarzy do agenta (też pi). Fajny dodatek do pracy z terminala |

### Pozostałe decyzje (bez zmian)

- **Orbi odpada**: brak multi-repo.
- **Polling, nie webhooki**: zero publicznych portów.
- **Tani autor, mocniejszy reviewer**: DeepSeek Flash pisze, reviewer z OpenRouter na mocniejszym modelu. Do tego twarde AC i CI jako bramka.
- **Osobny klucz OpenRouter z limitem kredytów** + limity tokenów w herdr-projects.

## Architektura

```
GitHub (kilkanaście repo)
   │ issues: label "start", komentarze        PR: komentarze, review, checki
   ▼                                           ▲ │
┌────────────────────────── VPS ───────────────┼─┼───────────────────┐
│ gh-bridge (Python, systemd, ~150 linii)      │ │                   │
│   label start  → herdr-projects add --delegate --merge.closes N    │
│   issue comment→ herdr agent prompt <pane taska>                   │
│        │                                     │ │                   │
│        ▼                                     │ ▼                   │
│ herdr-projects (keeper)  ── draft PR, review, wake na feedback ────┤
│   ├─ api#12  space+worktree → pi (deepseek-v4.1-flash)             │
│   ├─ web#45  space+worktree → pi                                   │
│   └─ reviewer → pi (mocniejszy model)                              │
│ herdr server (trwałe sesje, web UI przez tailscale)                │
└────────────────────────────────────────────────────────────────────┘
   merge (Twoje „tak” z boardu / telefonu)
   ▼
GitHub Actions w repo: push main → tag vX.Y.Z → build obrazu → deploy
```

## Bezpieczeństwo

- **Komentarze w PR budzą agenta tylko od zalogowanego usera `gh`.** Most stosuje tę samą zasadę dla issue (allowlista = Ty / zespół). Konsekwencja: `gh` na VPS działa na koncie, którego komentarze mają sterować agentem. Fine-grained token tylko na wybrane repo.
- Agenci w herdr-projects mają **domyślnie pełne uprawnienia** (pi i tak nie potrafi pytać). Dlatego: osobny user `agent` bez sudo, bez sekretów prod na VPS, branch protection + wymagane CI, opcjonalnie `sbx`/devcontainer (`herdr-devcontainer`) do izolacji.
- `merge.by you` i `autopilot.approve no` zostawiamy domyślnie, wtedy nic nie merguje się bez Ciebie.

## Do zweryfikowania w spike'u

1. pi + OpenRouter w herdr-projects: `--worker.agent pi/openrouter/deepseek/deepseek-v4.1-flash` (format `pi/<provider>/<model>`, lista z `pi --list-models`).
2. `herdr-projects add` z CLI (bez Coordinatora) + `--delegate` + `--merge.closes`: czy brief agenta zawiera treść issue, czy trzeba ją przekazać w opisie taska.
3. Mapowanie task → pane do odpowiedzi w issue (`herdr-projects list` / `show`, czy jest `--json`).
4. Keeper jako usługa na headless VPS (systemd user + `loginctl enable-linger`) i zachowanie po restarcie.
5. Draft PR vs normalny PR: jak oznaczyć „gotowe do review człowieka” w GitHubie.
6. Koszt: Watcher robi małe wywołanie modelu ~co minutę na działającego agenta. Sprawdzić, jakim modelem i ile to kosztuje.

## Źródła

- [herdr-projects](https://github.com/eliasstravik/herdr-projects) · [docs/how-work-gets-done.md](https://github.com/eliasstravik/herdr-projects/blob/main/docs/how-work-gets-done.md) · [docs/automations.md](https://github.com/eliasstravik/herdr-projects/blob/main/docs/automations.md) · [docs/models.md](https://github.com/eliasstravik/herdr-projects/blob/main/docs/models.md) · [prs.rs](https://github.com/eliasstravik/herdr-projects/blob/main/crates/flow/src/housekeeping/prs.rs)
- [herdr-desk](https://github.com/duyet/herdr-desk) · [herdr-plugin-odysseus](https://github.com/jpolec/herdr-plugin-odysseus) · [herdr-reviewr](https://github.com/persiyanov/herdr-reviewr) · [herdr-devcontainer](https://github.com/gambtho/herdr-devcontainer)
- [herdr – CLI/socket API](https://herdr.dev/docs/socket-api/) · [herdr – agents](https://herdr.dev/docs/agents/)
- [OpenRouter – DeepSeek V4.1 Flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash)
