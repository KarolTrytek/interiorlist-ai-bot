"""Pobieranie wszystkich stron w jednym przebiegu — szybko, ale grzecznie dla sklepów.

Faza 1 (równolegle): każdy sklep ma swój wątek i swoje tempo — kilka sklepów naraz,
ale do jednego sklepu zapytania idą po kolei, z przerwą. Sklep, który przestaje
odpowiadać, jest pomijany do końca przebiegu (zamiast czekać na każdy link osobno).
Gdy sklep odpowiada „za dużo zapytań” (429), czekamy i zwalniamy tempo.

Faza 2 (po kolei): przeglądarka tylko dla stron, których nie dało się odczytać,
i nie dla sklepów, które blokują ją już drugi raz z rzędu.
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import config, sklepy
from .ekstrakcja import WynikStrony, analizuj_html
from .strona import Strona, domena, pobierz_http, pobierz_przegladarka, zamknij_przegladarke

log = logging.getLogger(__name__)

MAX_SKLEPOW_NARAZ = 8
MAX_CZEKANIA_429_S = 45


def _odczytaj(strona: Strona) -> WynikStrony | None:
    """Dane ze strony, a gdy ich brak — z API platformy sklepu."""
    if not strona.html:
        return None
    wynik = analizuj_html(strona.html, config.PROG_MALO_SZTUK)
    if wynik.jest_produkt or strona.nie_istnieje or strona.zablokowana:
        return wynik
    z_api = sklepy.z_api(strona.url_koncowy or strona.url, strona.html)
    return z_api or wynik


def _sklep_po_kolei(adresy: list[str]) -> dict[str, tuple[Strona, WynikStrony | None]]:
    wyniki: dict[str, tuple[Strona, WynikStrony | None]] = {}
    przerwa = config.PRZERWA_MIEDZY_STRONAMI_S
    bez_odpowiedzi = 0
    for i, url in enumerate(adresy):
        if bez_odpowiedzi >= 2:
            s = Strona(url=url, blad="sklep nie odpowiada na połączenia z serwera bota", brak_polaczenia=True,
                       pominieta=True)
            wyniki[url] = (s, None)
            continue
        s = pobierz_http(url)
        if s.kod == 429:
            try:
                czekaj = min(int(s.retry_after), MAX_CZEKANIA_429_S)
            except ValueError:
                czekaj = 20
            time.sleep(czekaj)
            przerwa = min(przerwa * 2, 20)  # sklep prosi o wolniejsze tempo
            s = pobierz_http(url)
        bez_odpowiedzi = bez_odpowiedzi + 1 if s.brak_polaczenia else 0
        try:
            wyniki[url] = (s, _odczytaj(s))
        except Exception:  # noqa: BLE001 — jedna dziwna strona nie może zatrzymać sklepu
            log.exception("Błąd odczytu %s", url)
            wyniki[url] = (s, None)
        if i < len(adresy) - 1:
            time.sleep(przerwa + random.uniform(0, 1.0))
    return wyniki


def _potrzebna_przegladarka(strona: Strona, wynik: WynikStrony | None) -> bool:
    if strona.pominieta or strona.nie_istnieje:
        return False
    if strona.blad or strona.zablokowana:
        return True
    return wynik is None or not wynik.jest_produkt


def _wyglada_na_usuniety(strona: Strona, wynik: WynikStrony | None) -> bool:
    if strona.nie_istnieje:
        return True
    if wynik is not None and not wynik.jest_produkt and (wynik.soft_404 or strona.na_strone_glowna):
        return True
    return False


def pobierz_wszystkie(adresy: list[str]) -> tuple[dict[str, tuple[Strona, WynikStrony | None]], Counter]:
    """Zwraca (url -> (strona, wynik), licznik zablokowanych/niedostępnych linków na sklep)."""
    unikalne = list(dict.fromkeys(adresy))
    po_sklepach: dict[str, list[str]] = defaultdict(list)
    for url in unikalne:
        po_sklepach[domena(url)].append(url)
    log.info("Faza 1: %d linków w %d sklepach (do %d sklepów naraz)…", len(unikalne), len(po_sklepach),
             MAX_SKLEPOW_NARAZ)

    wyniki: dict[str, tuple[Strona, WynikStrony | None]] = {}
    start = time.monotonic()
    # Najwięksi najpierw, żeby nie czekać na końcu na jeden długi sklep.
    kolejnosc = sorted(po_sklepach.values(), key=len, reverse=True)
    with ThreadPoolExecutor(max_workers=MAX_SKLEPOW_NARAZ) as pula:
        for czesc in pula.map(_sklep_po_kolei, kolejnosc):
            wyniki.update(czesc)
    log.info("Faza 1 gotowa w %d s.", int(time.monotonic() - start))

    do_przegladarki = [u for u in unikalne if _potrzebna_przegladarka(*wyniki[u])]
    if config.UZYJ_PRZEGLADARKI and do_przegladarki:
        log.info("Faza 2: przeglądarka dla %d stron…", len(do_przegladarki))
        porazki: Counter = Counter()
        try:
            for url in do_przegladarki:
                sklep = domena(url)
                if porazki[sklep] >= 2:
                    continue  # ten sklep blokuje też przeglądarkę — nie męczymy go dalej
                pierwsza, _ = wyniki[url]
                druga = pobierz_przegladarka(url)
                if druga.blad or druga.zablokowana:
                    porazki[sklep] += 1
                    if druga.blad and not pierwsza.blad and pierwsza.html:
                        continue  # zostaje wynik zwykłego zapytania
                else:
                    porazki[sklep] = 0
                wyniki[url] = (druga, _odczytaj(druga) if druga.html else None)
                time.sleep(1 + random.uniform(0, 1.0))
        finally:
            zamknij_przegladarke()

    # Faza 3: strony, które wyglądają na usunięte, sprawdzamy jeszcze raz po chwili —
    # żeby chwilowe 404 czy przerwa w sklepie nie wywołały alarmu „zniknął”.
    podejrzane = [u for u in unikalne if _wyglada_na_usuniety(*wyniki[u])]
    if podejrzane and config.PONOWNE_SPRAWDZENIE_S > 0:
        log.info("Faza 3: ponowne sprawdzenie %d stron, które wyglądają na usunięte (za %d s)…",
                 len(podejrzane), int(config.PONOWNE_SPRAWDZENIE_S))
        time.sleep(config.PONOWNE_SPRAWDZENIE_S)
        for url in podejrzane:
            s = pobierz_http(url)
            w = _odczytaj(s) if s.html else None
            if not _wyglada_na_usuniety(s, w) and not s.blad and not s.zablokowana:
                log.info("   ↺ %s wróciła przy ponownym sprawdzeniu", url)
                wyniki[url] = (s, w)
            time.sleep(1 + random.uniform(0, 1.0))

    problemowe: Counter = Counter()
    for url, (s, w) in wyniki.items():
        if s.zablokowana or s.brak_polaczenia or (s.blad and not s.html):
            problemowe[domena(url)] += 1
    log.info("Pobieranie zakończone w %d s.", int(time.monotonic() - start))
    return wyniki, problemowe
