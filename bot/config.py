"""Ustawienia bota. Wszystko, co może się zmieniać, czytamy ze zmiennych środowiskowych
(w GitHub Actions: Settings → Secrets and variables → Actions)."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def _env(nazwa: str, domyslna: str = "") -> str:
    # Pusta zmienna (np. nieustawiona zmienna repozytorium w GitHub Actions) = wartość domyślna.
    return (os.getenv(nazwa) or "").strip() or domyslna


def _env_bool(nazwa: str, domyslna: bool) -> bool:
    wartosc = os.getenv(nazwa)
    if wartosc is None or wartosc.strip() == "":
        return domyslna
    return wartosc.strip().lower() in ("1", "true", "tak", "yes", "on")


def _env_float(nazwa: str, domyslna: float) -> float:
    try:
        return float(_env(nazwa) or domyslna)
    except ValueError:
        return domyslna


# --- Airtable (TYLKO ODCZYT) ---
AIRTABLE_TOKEN = _env("AIRTABLE_TOKEN")
BASE_ID = _env("BASE_ID")
TABELA_PRODUKTOW = _env("AIRTABLE_TABELA", "Products")
# Opcjonalnie ID tabeli (tbl...), żeby w mailu był bezpośredni link do rekordu.
TABELA_PRODUKTOW_ID = _env("AIRTABLE_TABELA_ID")
# Formuła wybierająca produkty do sprawdzenia.
FORMULA_PRODUKTOW = _env("AIRTABLE_FORMULA", "NOT({Affiliate Link} = '')")

# Nazwy pól w tabeli Products (takie jak w starym bocie).
POLE_NAZWA = "Product Name"
POLE_LINK = "Affiliate Link"
POLE_CENA = "Price"
POLE_WYMIAR = "Dimension"
POLE_STATUS = "Availability Status"
POLE_ZDJECIE = "Product Image"
POLE_ZDJECIE_POKOJU = "Cover Render Image"

# Wartości pola "Availability Status" w Airtable.
STATUS_DOSTEPNY = "Dostępny"
STATUS_MALO = "Mało sztuk"
STATUS_WYCOFANY = "Wycofany"

# Rozmiar, który ZAWSZE wybieramy dla danego typu produktu (dopasowanie po nazwie z Airtable).
# Łóżka na listach są zawsze 160x200 — bot sprawdza cenę właśnie tego rozmiaru, a jeśli sklep
# jej nie pokazuje w danych strony, pomija porównanie ceny (dostępność sprawdza normalnie).
STALE_WARIANTY: list[tuple[str, str]] = [
    # „Łóżko Theo”, „Rama łóżka X”, „Łóżko tapicerowane …” — ale nie „Narzuta na łóżko”
    (r"^(?:\S+\s+)?(?<!na )(?<!do )(?<!pod )(?<!przy )(?<!nad )ł[oó]żk[oa]\b", "160x200"),
]

# --- Progi ---
# Zmiana ceny jest zgłaszana, gdy różnica przekracza OBA progi.
PROG_CENY_PROCENT = _env_float("PROG_CENY_PROCENT", 2.0)
PROG_CENY_ZL = _env_float("PROG_CENY_ZL", 5.0)
# Poniżej tylu sztuk produkt traktujemy jako "Mało sztuk".
PROG_MALO_SZTUK = int(_env_float("PROG_MALO_SZTUK", 20))
# Ile kolejnych nieudanych sprawdzeń, zanim zgłosimy "nie udało się sprawdzić".
PROG_BLEDOW = int(_env_float("PROG_BLEDOW", 3))
# Ile przebiegów z rzędu produkt musi wyglądać na wycofany, zanim wyślemy alarm
# (chroni przed chwilowym 404 czy przerwą w sklepie; przy 4 przebiegach dziennie = alarm w ciągu ~5 h).
PROG_WYCOFANIA = int(_env_float("PROG_WYCOFANIA", 2))

# --- Pobieranie stron ---
PRZERWA_MIEDZY_STRONAMI_S = _env_float("PRZERWA_S", 2.5)
TIMEOUT_S = _env_float("TIMEOUT_S", 25)
# Zapasowa przeglądarka (Playwright) dla stron, które blokują zwykłe zapytania
# albo budują treść JavaScriptem.
UZYJ_PRZEGLADARKI = _env_bool("PRZEGLADARKA", True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# --- Zamienniki (Claude Code na subskrypcji, token z `claude setup-token`) ---
ZAMIENNIKI_WLACZONE = _env_bool("ZAMIENNIKI", True)
MODEL_ZAMIENNIKOW = _env("MODEL_ZAMIENNIKOW", "opus")
MAX_ZAMIENNIKOW_NA_PRZEBIEG = int(_env_float("MAX_ZAMIENNIKOW_NA_PRZEBIEG", 5))
TIMEOUT_ZAMIENNIKOW_S = int(_env_float("TIMEOUT_ZAMIENNIKOW_S", 900))
MAX_TUR_ZAMIENNIKOW = int(_env_float("MAX_TUR_ZAMIENNIKOW", 60))
# Łączny limit czasu na zamienniki w jednym przebiegu (reszta poczeka na kolejny).
BUDZET_ZAMIENNIKOW_MIN = _env_float("BUDZET_ZAMIENNIKOW_MIN", 45)

# Sklepy, w których wolno szukać zamienników: domena (bez www) -> nazwa w mailu.
# Ścieżka po domenie (np. "ikea.com/pl") zawęża do polskiej wersji sklepu.
SKLEPY: dict[str, str] = {
    "agatameble.pl": "Agata Meble",
    "jysk.pl": "JYSK",
    "beliani.pl": "Beliani",
    "sklum.com/pl": "Sklum",
    "lectus24.pl": "Lectus24",
    "brw.pl": "Black Red White",
    "selsey.pl": "Selsey",
    "allegro.pl": "Allegro",
    "ikea.com/pl": "IKEA",
    "meble-mwm.pl": "Meble MWM",
    "eyeon.design": "EyeOn Design",
    "comfort.pl": "Comfort",
    "westwing.pl": "Westwing",
    "meble-bogart.pl": "Bogart",
    "miamo.pl": "Miamo",
}

# --- Mail ---
SMTP_SERWER = _env("SMTP_SERWER", "smtp.gmail.com")
SMTP_PORT = int(_env_float("SMTP_PORT", 587))
MAIL_NADAWCA = _env("GMAIL_SENDER", "interiorlist.bot@gmail.com")
# Stary bot miał literówkę w nazwie zmiennej — obsługujemy obie.
MAIL_HASLO = _env("GMAIL_APP_PASSWORD") or _env("GMAIL_APPPASWORD")
MAIL_ODBIORCY = [a.strip() for a in _env("GMAIL_RECEIVER").split(",") if a.strip()]
# Raz dziennie (pierwszy przebieg po tej godzinie) przypomnienie o wciąż otwartych sprawach.
PRZYPOMNIENIE_CODZIENNE = _env_bool("PRZYPOMNIENIE_CODZIENNE", True)
GODZINA_PRZYPOMNIENIA = int(_env_float("GODZINA_PRZYPOMNIENIA", 7))

STREFA = ZoneInfo("Europe/Warsaw")
SCIEZKA_STANU = _env("SCIEZKA_STANU", "stan/stan.json")

# Wygląd maila (wytyczne Interior List).
LOGO_URL = (
    "https://res.cloudinary.com/dpkcxbwkk/image/upload/v1785595419/"
    "logo_strona_net_zak%C5%82adka_g%C5%82%C3%B3wna_edne62.png"
)
