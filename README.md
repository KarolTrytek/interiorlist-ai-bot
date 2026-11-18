# Bot produktów Interior List

Kilka razy dziennie sprawdza ceny i dostępność wszystkich produktów z Airtable na stronach sklepów.
**Niczego nie zmienia w Airtable.** Gdy coś się zmieni, wysyła maila z listą, a dla produktów, które zniknęły
lub są niedostępne, dołącza propozycje zamienników przygotowane przez Claude.

## Jak to działa

```
GitHub Actions (4× dziennie, darmowe minuty)
 1. Airtable → lista produktów (tylko odczyt)
 2. Strona sklepu → cena i dostępność z danych, które sklep publikuje dla Google (JSON-LD),
    zapasowo mikrodane, meta tagi i frazy typu „ostatnie sztuki”, „oferta zakończona”.
    Tu nie ma AI — to jest darmowe, szybkie i nie zmyśla.
 3. Porównanie z Airtable → zmiany: zniknął / niedostępny / mało sztuk / zmiana ceny / wrócił do sprzedaży
 4. Dla zniknętych/niedostępnych: Claude Code na Twojej subskrypcji szuka zamienników
    (zdjęcie produktu + wizualizacja pokoju, tylko sklepy z listy). Każdy link bot sprawdza potem sam.
 5. Mail tylko wtedy, gdy jest coś NOWEGO. Raz dziennie (od 7:00) przypomnienie o sprawach,
    które wciąż czekają na poprawę w Airtable. Jeśli mail nie wyjdzie, sprawy wracają w kolejnym przebiegu.
 6. Pamięć bota → plik stan/stan.json w repozytorium.
```

Co się zmieniło względem starego bota:

| Stary bot | Nowy bot |
|---|---|
| Zapisywał ceny i statusy do Airtable | Tylko czyta, raport w mailu |
| Gemini czytało całą stronę, żeby znaleźć cenę | Cena z danych strukturalnych sklepu, bez AI |
| Zamienniki jako HTML pisany przez AI, linki bywały zmyślone | Sztywny JSON, AI może otwierać tylko sklepy z listy, każdy link weryfikowany w Pythonie |
| Mail przy każdym uruchomieniu | Mail tylko przy nowych zmianach + przypomnienie raz dziennie |
| Ręczne uruchamianie | GitHub Actions, 4× dziennie |

## Wdrożenie (jednorazowo, ok. 20 minut)

### 1. Wrzuć pliki do repozytorium

Najprościej: nowe, **prywatne** repozytorium (np. `interior-list-bot`) z zawartością tego folderu.
Możesz też podmienić pliki w obecnym repo bota.

### 2. Token Airtable tylko do odczytu

[airtable.com/create/tokens](https://airtable.com/create/tokens) → Create token:
- scopes: `data.records:read` (+ opcjonalnie `schema.bases:read` — wtedy w mailu będą linki „Popraw w Airtable”),
- access: tylko baza Interior List.

Bez `data.records:write` bot technicznie nie może niczego zmienić w Airtable.

### 3. Token Claude z Twojej subskrypcji

Na swoim komputerze (z zainstalowanym Claude Code):

```bash
claude setup-token
```

Zaloguj się w przeglądarce, skopiuj wypisany token. Jest ważny rok — ustaw sobie przypomnienie.
Zamienniki będą liczyć się do limitów Twojej subskrypcji (szukamy ich tylko raz na wycofany produkt, max 5 na przebieg).

### 4. Sekrety w GitHub

Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Nazwa | Wartość |
|---|---|
| `AIRTABLE_TOKEN` | token z kroku 2 |
| `BASE_ID` | `appV5Ry15hP6kmkBc` |
| `GMAIL_APP_PASSWORD` | hasło aplikacji skrzynki interiorlist.bot@gmail.com |
| `GMAIL_RECEIVER` | adres(y) odbiorców, po przecinku |
| `CLAUDE_CODE_OAUTH_TOKEN` | token z kroku 3 |

Opcjonalnie w zakładce **Variables**:

| Nazwa | Domyślnie | Po co |
|---|---|---|
| `AIRTABLE_TABELA_ID` | — | ID tabeli Products (`tbl…`) dla linków w mailu, jeśli token nie ma `schema.bases:read` |
| `MODEL_ZAMIENNIKOW` | `opus` | `sonnet`, jeśli chcesz oszczędzać limity subskrypcji |
| `PRZEGLADARKA` | włączona | `0` wyłącza zapasową przeglądarkę (szybciej, ale więcej „nie udało się sprawdzić”) |
| `GMAIL_SENDER` | interiorlist.bot@gmail.com | inny nadawca |

### 5. Pierwsze uruchomienie (testowe)

Actions → **Sprawdzanie produktów** → Run workflow → `limit: 10`, zaznacz „Nie wysyłaj maila”.
Po zakończeniu w podsumowaniu przebiegu jest plik **podglad-maila** — tak będzie wyglądał raport.

Potem uruchom bez limitu. Pierwszy pełny przebieg zgłosi wszystkie obecne rozbieżności jako „nowe”
i poszuka zamienników (do 5 na przebieg, reszta w kolejnych). Od drugiego przebiegu przychodzą już tylko zmiany.

## Testowanie lokalne

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium

# Co bot odczytuje z konkretnej strony (bez Airtable):
python sprawdz.py --url "https://www.jysk.pl/..."

# 10 produktów, podgląd maila do pliku, bez wysyłki i bez zapisu stanu:
AIRTABLE_TOKEN=... BASE_ID=... python sprawdz.py --limit 10 --podglad raport.html --bez-maila

# Zamienniki dla jednego produktu (wymaga zalogowanego Claude Code):
AIRTABLE_TOKEN=... BASE_ID=... python sprawdz.py --zamienniki-dla recXXXXXXXXXXXXXX

# Testy:
python -m pytest -q
```

Warto przepuścić przez `--url` po jednym produkcie z każdego sklepu — od razu widać,
które sklepy publikują dane strukturalne, a które wymagają przeglądarki albo blokują boty.

## Ustawienia, które możesz chcieć zmienić

W `bot/config.py`:
- **progi zmiany ceny** — domyślnie zgłaszamy, gdy różnica przekracza 5 zł **i** 2%,
- **„mało sztuk”** — poniżej 20 sztuk,
- **potwierdzenie wycofania** — produkt musi wyglądać na wycofany w 2 przebiegach z rzędu (`PROG_WYCOFANIA`),
  zanim przyjdzie alarm. Chroni przed chwilowym 404 albo przerwą w sklepie; przy 4 przebiegach dziennie
  alarm przychodzi najpóźniej po kilku godzinach. Ustaw `1`, jeśli wolisz alarm od razu,
- **frazy na stronie** — „oferta zakończona”, „wycofany z oferty” wystarczą, ale słabe frazy
  („chwilowo niedostępny”) liczą się tylko wtedy, gdy strona nie podaje ceny. Fałszywe „wycofany”
  jest gorsze niż przeoczenie, bo architekt podmieniłby dobry produkt,
- **lista sklepów** dla zamienników (`SKLEPY`),
- **nazwy pól** w Airtable (jeśli kiedyś je zmienisz).

Godziny uruchomień: `.github/workflows/sprawdzanie-produktow.yml`, linia `cron` (czas UTC).

## Koszty

- **GitHub Actions**: prywatne repo ma 2000 darmowych minut miesięcznie. Przebieg przy ~150 produktach
  to ok. 8–10 minut, czyli ~1000–1200 minut przy 4 przebiegach dziennie. Przy większej liczbie produktów
  zmniejsz liczbę godzin w `cron` albo zrób repo publiczne (wtedy minuty są bez limitu — kod nie zawiera
  żadnych haseł, wszystko jest w sekretach).
- **Claude**: zamienniki liczą się do Twojej subskrypcji, bez dodatkowych opłat.
- **Airtable, Gmail**: bez zmian.

## Gdy coś nie działa

- **„Nie udało się sprawdzić” przy jednym sklepie** — sklep blokuje automaty. Bot zgłasza to dopiero po
  3 nieudanych próbach z rzędu, żeby nie spamować przy chwilowych problemach.
- **Brak zamienników, w logu „Claude Code niedostępny”** — brak sekretu `CLAUDE_CODE_OAUTH_TOKEN` albo token wygasł
  (ważny rok — wygeneruj nowy `claude setup-token`).
- **Workflow nie startuje o czasie** — GitHub potrafi opóźnić zaplanowane uruchomienia o kilkanaście minut.
- **Chcesz, żeby bot zapomniał wszystko i zaczął od nowa** — usuń plik `stan/stan.json`.
