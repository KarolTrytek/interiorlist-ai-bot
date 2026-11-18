"""Pobieranie stron sklepów: najpierw zwykłe zapytanie HTTP, w razie blokady
lub pustej strony — zapasowo prawdziwa przeglądarka (Playwright)."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)

_ZNACZNIKI_BLOKADY = [
    "captcha",
    "datadome",
    "cf-chl",
    "just a moment",
    "attention required",
    "access denied",
    "are you a robot",
    "jesteś robotem",
    "potwierdź, że jesteś człowiekiem",
    "px-captcha",
]


@dataclass
class Strona:
    url: str
    url_koncowy: str = ""
    kod: int | None = None
    html: str = ""
    blad: str = ""
    zablokowana: bool = False
    przez_przegladarke: bool = False
    retry_after: str = ""
    brak_polaczenia: bool = False  # serwer w ogóle nie odpowiedział (timeout, zerwane połączenie)
    pominieta: bool = False  # nie sprawdzana, bo sklep wcześniej w tym przebiegu nie odpowiadał

    @property
    def nie_istnieje(self) -> bool:
        return self.kod in (404, 410)

    @property
    def na_strone_glowna(self) -> bool:
        """Przekierowanie na stronę główną sklepu = produkt zwykle usunięty."""
        if not self.url_koncowy:
            return False
        glowne = ("", "pl", "pl/pl", "pl-pl")
        poczatek, koniec = urlparse(self.url), urlparse(self.url_koncowy)
        # Link z produktem w parametrach (np. /?product_id=5) to nie przekierowanie.
        if poczatek.path.strip("/") in glowne:
            return False
        return koniec.path.strip("/") in glowne and not koniec.query


def _wyglada_na_blokade(kod: int | None, html: str) -> bool:
    if kod in (401, 403, 429, 503):
        return True
    poczatek = (html or "")[:6000].lower()
    if len(html or "") < 15000 and any(z in poczatek for z in _ZNACZNIKI_BLOKADY):
        return True
    return False


_watek = threading.local()  # osobna sesja HTTP dla każdego wątku pobierającego


def _pobierz_sesje() -> requests.Session:
    _sesja = getattr(_watek, "sesja", None)
    if _sesja is None:
        _sesja = _watek.sesja = requests.Session()
        _sesja.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6",
                "Cache-Control": "no-cache",
            }
        )
    return _sesja


def pobierz_http(url: str) -> Strona:
    strona = Strona(url=url)
    try:
        odp = _pobierz_sesje().get(url, timeout=config.TIMEOUT_S, allow_redirects=True)
        strona.kod = odp.status_code
        strona.url_koncowy = odp.url
        if not odp.encoding or odp.encoding.lower() == "iso-8859-1":
            odp.encoding = odp.apparent_encoding or "utf-8"
        strona.html = odp.text
        strona.zablokowana = _wyglada_na_blokade(odp.status_code, odp.text)
        strona.retry_after = odp.headers.get("Retry-After", "")
    except requests.RequestException as e:
        strona.blad = krotki_blad(e)
        strona.brak_polaczenia = isinstance(e, (requests.ConnectionError, requests.Timeout))
    return strona


def krotki_blad(e: Exception) -> str:
    """Jednolinijkowy opis błędu (bez wielolinijkowych logów Playwrighta)."""
    tekst = re.sub(r" at https?://\S+", "", str(e).split("\n")[0])
    if "Timeout" in type(e).__name__ or "timeout" in tekst.lower():
        return f"{type(e).__name__}: sklep nie odpowiedział w {int(config.TIMEOUT_S)} s"
    if "RemoteDisconnected" in tekst or "Connection aborted" in tekst:
        return "sklep zerwał połączenie"
    return f"{type(e).__name__}: {tekst}"[:200]


# ---------------------------------------------------------------- przeglądarka

_przegladarka = None
_playwright = None


def _uruchom_przegladarke():
    global _przegladarka, _playwright
    if _przegladarka is not None:
        return _przegladarka
    from playwright.sync_api import sync_playwright  # import tylko gdy potrzebny

    _playwright = sync_playwright().start()
    _przegladarka = _playwright.chromium.launch(headless=True)
    return _przegladarka


def zamknij_przegladarke() -> None:
    global _przegladarka, _playwright
    try:
        if _przegladarka is not None:
            _przegladarka.close()
        if _playwright is not None:
            _playwright.stop()
    except Exception:  # noqa: BLE001
        pass
    _przegladarka = _playwright = None


def pobierz_przegladarka(url: str) -> Strona:
    strona = Strona(url=url, przez_przegladarke=True)
    try:
        przegladarka = _uruchom_przegladarke()
        kontekst = przegladarka.new_context(
            user_agent=config.USER_AGENT,
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1366, "height": 900},
        )
        try:
            karta = kontekst.new_page()
            odp = karta.goto(url, wait_until="domcontentloaded", timeout=int(config.TIMEOUT_S * 1000) + 15000)
            try:
                karta.wait_for_load_state("networkidle", timeout=12000)
            except Exception:  # noqa: BLE001 - część sklepów nigdy nie "uspokaja" sieci
                pass
            strona.kod = odp.status if odp else None
            strona.url_koncowy = karta.url
            strona.html = karta.content()
            strona.zablokowana = _wyglada_na_blokade(strona.kod, strona.html)
        finally:
            kontekst.close()
    except Exception as e:  # noqa: BLE001
        strona.blad = f"przeglądarka: {krotki_blad(e)}"
        strona.brak_polaczenia = "ERR_" in str(e) or "Timeout" in type(e).__name__
    return strona


def pobierz(url: str, potrzebna_przegladarka=None) -> Strona:
    """Pobiera stronę. `potrzebna_przegladarka(strona) -> bool` decyduje, czy wynik
    zwykłego zapytania jest na tyle słaby, że warto spróbować przeglądarką."""
    strona = pobierz_http(url)
    if not config.UZYJ_PRZEGLADARKI or strona.nie_istnieje:
        return strona
    slaba = bool(strona.blad) or strona.zablokowana
    if not slaba and potrzebna_przegladarka is not None:
        slaba = potrzebna_przegladarka(strona)
    if not slaba:
        return strona
    log.info("   ↻ ponawiam przez przeglądarkę (%s)", strona.blad or f"HTTP {strona.kod}")
    druga = pobierz_przegladarka(url)
    if druga.blad:
        log.info("   ✗ przeglądarka też nie dała rady: %s", druga.blad)
    elif druga.zablokowana:
        log.info("   ✗ przeglądarka: sklep blokuje także ją (HTTP %s)", druga.kod)
    else:
        log.info("   ✓ przeglądarka: strona pobrana (HTTP %s)", druga.kod)
    # Bierzemy wynik przeglądarki, chyba że ona też poległa, a HTTP coś zwrócił.
    if druga.blad and not strona.blad and strona.html:
        return strona
    return druga


def domena(url: str) -> str:
    host = urlparse(url).hostname or ""
    return re.sub(r"^www\.", "", host.lower())
