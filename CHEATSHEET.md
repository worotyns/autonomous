# autonomous: ściąga

**Co to robi:** piszesz issue na GitHubie, dajesz label, a agent na serwerze robi resztę: kod, testy, PR i review. Ty tylko odpowiadasz na pytania i akceptujesz.

**Słowniczek:** *issue* to zadanie na GitHubie, *label* to etykieta, która steruje agentem (`start`, `consult`), *PR* to propozycja zmian w kodzie od agenta, a *agent* (pi) to program, który pisze kod na serwerze.

## Zlecenie zadania (codziennie)

1. **New issue** → szablon **Agent task**.
2. Wypełnij **Acceptance criteria**: każdy punkt to jedna rzecz do sprawdzenia.
   - ✅ `div(a, 0) rzuca ZeroDivisionError`
   - ❌ `ma działać dobrze`
3. Dodaj label **`start`**. Za ~1 min pojawi się `agent:working`.
4. Czekaj na `agent:ready` i link do PR → przejrzyj PR.
5. Wpisz w issue **`/approve`**. Merge, tag i release robią się same.

**Nie wiesz jeszcze, co dokładnie chcesz?** Daj label **`consult`** zamiast `start`. Agent przeczyta kod i zaproponuje opcje w komentarzach. Gdy już wiesz, napisz „spisz to”, a potem dodaj **`start`**.

## Komentarze

| Gdzie | Co się dzieje |
|---|---|
| w **issue** | agent dostaje Twój komentarz po ~90 s ciszy (zdąż dopisać) |
| w **PR** | agent poprawia kod i pushuje |
| `/approve` w issue | merge (tylko gdy jest `agent:ready`) |
| `/stop` w issue | zatrzymuje agenta |

Agent słucha tylko Ciebie (konto z `GH_TOKEN`) i osób z `allow_authors`.

## Labele: co znaczą

| Label | Znaczy | Ty |
|---|---|---|
| `agent:consulting` | rozmowa, bez kodu | odpisuj w komentarzach |
| `agent:working` | agent pracuje | nic, czekaj |
| `agent:needs-input` | agent pyta | odpowiedz w komentarzu |
| `agent:ready` | PR gotowy, po review | przejrzyj → `/approve` |
| `agent:failed` | coś padło | komentarz z podpowiedzią albo `/stop` |
| `agent:done` | zmergowane | 🎉 |

Wersja releasu: label na **PR** `release:minor` / `release:major` / `release:none`. Bez labela robi się patch.

## Pierwszy setup (raz)

```bash
git clone https://github.com/worotyns/autonomous && cd autonomous
cp config/autonomous.example.toml config/autonomous.toml   # wpisz swoje repo
cp config/env.example config/env                           # OPENROUTER_API_KEY, GH_TOKEN
./deploy.sh root@<ip-serwera>
ssh root@<ip-serwera>  →  sudo -iu agent  →  cd ~/repos/<owner>/<repo> && pi   # raz, potem ctrl+c
repo-kit/apply.sh <owner>/<repo>     # PR z szablonami do Twojego repo, zmerguj go
```

## Na serwerze (`ssh root@<ip-serwera>`)

| Chcę | Wpisz |
|---|---|
| sprawdzić, czy wszystko działa | `sudo autonomous doctor` |
| zobaczyć zadania | `sudo autonomous status` |
| podejrzeć agenta na żywo | `sudo autonomous attach` (`ctrl+b a` = board, `ctrl+b q` = wyjdź, agent pracuje dalej) |
| logi (np. `start` nic nie robi) | `sudo autonomous logs` |
| po zmianie configu / nowe repo | `sudo autonomous update` |
| coś się zawiesiło | `sudo autonomous restart` |

Nowe repo: dopisz `[[repo]]` w `/etc/autonomous/autonomous.toml` i potem `update`. W oknie agenta nic nie pisz, chyba że chcesz go przejąć.

## 3 zasady

1. **Konkretne acceptance criteria** dają dobry wynik. Ogólniki dają zgadywanie.
2. **Na serwerze nie ma sekretów produkcyjnych**, bo agent ma pełny shell.
3. **Nic nie merguje się bez `/approve`.** Zawsze przejrzyj PR.
