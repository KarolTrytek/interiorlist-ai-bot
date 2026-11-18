"""Porównanie tego, co jest w Airtable, z tym, co jest na stronie sklepu.
Wynik to lista "problemów" do pokazania w mailu — nic nie jest zapisywane w Airtable."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config
from .airtable_odczyt import Produkt
from .ekstrakcja import (
    DOSTEPNY,
    MALO,
    NIEDOSTEPNY,
    NIEZNANY,
    WynikStrony,
    status_produktu,
    wybierz_oferte,
)
from .strona import Strona, domena

ZNIKNAL = "zniknal"
NIEDOST = "niedostepny"
MALO_SZTUK = "malo"
ZMIANA_CENY = "cena"
WROCIL = "wrocil"
BLAD = "blad"

# Kolejność w mailu (najpilniejsze na górze) i opisy.
KOLEJNOSC = [ZNIKNAL, NIEDOST, MALO_SZTUK, ZMIANA_CENY, WROCIL, BLAD]
ETYKIETY = {
    ZNIKNAL: "Zniknął ze sklepu",
    NIEDOST: "Niedostępny",
    MALO_SZTUK: "Mało sztuk",
    ZMIANA_CENY: "Zmiana ceny",
    WROCIL: "Wrócił do sprzedaży",
    BLAD: "Nie udało się sprawdzić",
}
# Typy, dla których szukamy zamienników.
WYMAGA_ZAMIENNIKOW = {ZNIKNAL, NIEDOST}

_NAZWY_STATUSOW = {
    DOSTEPNY: config.STATUS_DOSTEPNY,
    MALO: config.STATUS_MALO,
    NIEDOSTEPNY: "Niedostępny",
    NIEZNANY: "Nieznany",
}


@dataclass
class Problem:
    typ: str
    produkt: Produkt
    opis: str
    w_airtable: str = ""
    na_stronie: str = ""
    nowa_cena: float | None = None
    url_koncowy: str = ""
    uwaga: str = ""
    zdjecie: str = ""
    zamienniki: dict = field(default_factory=dict)

    @property
    def klucz(self) -> str:
        """Stały identyfikator — po nim poznajemy, czy problem jest nowy."""
        if self.typ in (ZNIKNAL, NIEDOST):
            return f"{self.produkt.id}:wycofany"
        return f"{self.produkt.id}:{self.typ}"

    @property
    def etykieta(self) -> str:
        return ETYKIETY[self.typ]


def zl(kwota: float | None) -> str:
    if kwota is None:
        return "brak ceny"
    calosc = f"{kwota:,.2f}".replace(",", " ").replace(".", ",")
    return calosc.removesuffix(",00") + " zł"


def _opis_airtable(p: Produkt) -> str:
    czesci = [p.status or config.STATUS_DOSTEPNY]
    if p.cena is not None:
        czesci.append(zl(p.cena))
    return " · ".join(czesci)


def staly_wariant(p: Produkt) -> str:
    """Rozmiar, który zawsze sprawdzamy dla tego typu produktu (np. łóżka -> 160x200), albo ""."""
    nazwa = p.nazwa.strip().lower()
    for wzor, wymiar in config.STALE_WARIANTY:
        if re.search(wzor, nazwa):
            return wymiar
    return ""


def istotna_zmiana(stara: float, nowa: float) -> bool:
    roznica = abs(nowa - stara)
    return roznica > config.PROG_CENY_ZL and (roznica / stara * 100) > config.PROG_CENY_PROCENT


def _wycofany_w_airtable(p: Produkt) -> bool:
    return p.status.strip().lower() == config.STATUS_WYCOFANY.lower()


def _dostepny_w_airtable(p: Produkt) -> bool:
    return p.status.strip().lower() == config.STATUS_DOSTEPNY.lower()


def ocen(p: Produkt, strona: Strona, wynik: WynikStrony | None) -> list[Problem]:
    sklep = domena(strona.url_koncowy or p.url)
    w_at = _opis_airtable(p)

    def problem(typ: str, opis: str, **kw) -> list[Problem]:
        return [Problem(typ=typ, produkt=p, opis=opis, w_airtable=w_at, url_koncowy=strona.url_koncowy, **kw)]

    # 1. Strona w ogóle nie istnieje albo nie dała się pobrać.
    if strona.nie_istnieje:
        if _wycofany_w_airtable(p):
            return []
        return problem(ZNIKNAL, f"Strona produktu zwraca błąd {strona.kod} — produkt usunięty ze sklepu {sklep}.",
                       na_stronie=f"Błąd {strona.kod}")
    if strona.blad and not strona.html:
        return problem(BLAD, f"Nie udało się połączyć ze sklepem ({strona.blad}).", na_stronie="Brak połączenia")
    if wynik is None:
        return problem(BLAD, "Nie udało się odczytać strony.", na_stronie="—")
    # Błąd serwera, przerwa techniczna, blokada (500/503/403/429…) — tekst takiej strony
    # („sklep chwilowo niedostępny”) nie mówi nic o produkcie.
    if strona.kod is not None and strona.kod >= 400:
        opis = (f"Sklep {sklep} zablokował automatyczne sprawdzenie (HTTP {strona.kod})." if strona.zablokowana
                else f"Sklep {sklep} zwrócił błąd HTTP {strona.kod}.")
        return problem(BLAD, opis, na_stronie=f"Błąd {strona.kod}")

    # 2. Strona jest, ale to nie jest strona produktu.
    if not wynik.jest_produkt:
        if strona.zablokowana:
            return problem(BLAD, f"Sklep {sklep} zablokował automatyczne sprawdzenie.",
                           na_stronie="Blokada sklepu")
        powod_zniknal = ""
        if wynik.soft_404:
            powod_zniknal = "Sklep pokazuje stronę „nie znaleziono”"
        elif strona.na_strone_glowna:
            powod_zniknal = "Link przekierowuje na stronę główną sklepu"
        if powod_zniknal:
            if _wycofany_w_airtable(p):
                return []
            return problem(ZNIKNAL, f"{powod_zniknal} — produkt prawdopodobnie usunięty.",
                           na_stronie=powod_zniknal)
        if wynik.status_z_tekstu == NIEZNANY:
            return problem(BLAD, "Strona nie zawiera danych produktu (ceny ani dostępności) — "
                                 "możliwe przekierowanie na kategorię albo zmiana strony sklepu.",
                           na_stronie="Brak danych produktu")

    # 3. Mamy dane produktu — status i cena.
    staly = staly_wariant(p)
    wymiar = staly or p.wymiar
    oferta = wybierz_oferte(wynik, wymiar, scisle=bool(staly))
    status, zrodlo = status_produktu(wynik, oferta, config.PROG_MALO_SZTUK)
    problemy: list[Problem] = []

    if status == NIEDOSTEPNY:
        if not _wycofany_w_airtable(p):
            problemy += problem(NIEDOST, f"Sklep oznacza produkt jako niedostępny ({zrodlo}).",
                                na_stronie=_NAZWY_STATUSOW[status])
        return problemy  # cena niedostępnego produktu nie ma znaczenia

    if status == MALO and _dostepny_w_airtable(p):
        problemy += problem(MALO_SZTUK, f"Kończy się w sklepie ({zrodlo}).", na_stronie=_NAZWY_STATUSOW[status])

    if status in (DOSTEPNY, MALO) and _wycofany_w_airtable(p):
        problemy += problem(WROCIL, f"W Airtable jest „Wycofany”, a sklep znowu go sprzedaje ({zrodlo}).",
                            na_stronie=_NAZWY_STATUSOW[status])

    # Cena.
    if p.cena:
        ceny = wynik.ceny
        cena_wariantu = oferta.cena if oferta is not None else None
        if cena_wariantu is not None and "konfiguratora" in oferta.nazwa and not 0.5 <= cena_wariantu / p.cena <= 2:
            cena_wariantu = None  # nieprawdopodobna cena z konfiguratora — nie ryzykujemy alarmu
        if staly and cena_wariantu is None:
            # Strona nie podaje ceny rozmiaru 160x200 (np. konfigurator „Skonfiguruj i kup”).
            # Lepiej nie porównywać niż zgłaszać cenę mniejszego rozmiaru.
            return problemy
        uwaga = ""
        if not staly and p.wymiar and len(wynik.ceny) == 1 and re.search(r"\d", p.wymiar):
            uwaga = f"Sprawdź, czy strona pokazuje wariant {p.wymiar} — sklep mógł podać cenę innego wariantu."
        if cena_wariantu is not None:
            if istotna_zmiana(p.cena, cena_wariantu):
                kierunek = "wzrosła" if cena_wariantu > p.cena else "spadła"
                problemy += problem(ZMIANA_CENY, f"Cena {kierunek}: {zl(p.cena)} → {zl(cena_wariantu)}.",
                                    na_stronie=zl(cena_wariantu), nowa_cena=cena_wariantu, uwaga=uwaga)
        elif ceny and not any(not istotna_zmiana(p.cena, c) for c in ceny):
            zakres = zl(ceny[0]) if len(ceny) == 1 else f"{zl(ceny[0])} – {zl(ceny[-1])}"
            problemy += problem(
                ZMIANA_CENY,
                f"Żaden wariant na stronie nie kosztuje {zl(p.cena)} (na stronie: {zakres}).",
                na_stronie=zakres,
                uwaga=f"Sprawdź wariant {p.wymiar}." if p.wymiar else "",
            )
    return problemy
