"""Bot produktów Interior List.

Sprawdza ceny i dostępność wszystkich produktów z Airtable na stronach sklepów,
NICZEGO nie zmienia w Airtable, a gdy coś się zmieni — wysyła maila z listą
i (dla wycofanych produktów) propozycjami zamienników przygotowanymi przez Claude.

Przykłady:
    python sprawdz.py                          # normalny przebieg (tak działa w GitHub Actions)
    python sprawdz.py --url "https://..."      # test jednej strony: co bot z niej odczytuje
    python sprawdz.py --limit 10 --podglad raport.html --bez-maila
    python sprawdz.py --zamienniki-dla recXXXXXXXXXXXXXX
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from bot import config
from bot.airtable_odczyt import id_tabeli, link_do_rekordu, pobierz_produkty
from bot.ekstrakcja import analizuj_html, status_produktu, wybierz_oferte, zdjecie_og
from bot.mail import odmiana, wyslij, zbuduj
from bot.porownanie import BLAD, WYMAGA_ZAMIENNIKOW, ZMIANA_CENY, Problem, istotna_zmiana, ocen, zl
from bot.stan import Stan, teraz
from bot.pobieranie import pobierz_wszystkie
from bot.strona import Strona, domena, pobierz
from bot import sklepy
from bot import zamienniki as modul_zamiennikow

log = logging.getLogger("bot")
MAX_PROB_ZAMIENNIKOW = 3
KATALOG_STRON = "strony-do-sprawdzenia"


def test_url(url: str) -> None:
    strona, wynik = pobierz_i_odczytaj(url)
    print(f"HTTP: {strona.kod}  |  końcowy adres: {strona.url_koncowy}")
    print(f"Przez przeglądarkę: {strona.przez_przegladarke}  |  blokada: {strona.zablokowana}  |  błąd: {strona.blad or '—'}")
    if wynik is None:
        return
    print(f"Źródło danych: {wynik.zrodlo or 'brak'}  |  nazwa: {wynik.nazwa_produktu or '—'}")
    print(f"Strona produktu: {wynik.jest_produkt}  |  „nie znaleziono”: {wynik.soft_404}")
    for o in wynik.oferty[:20]:
        print(f"   • {zl(o.cena):>12}  {o.status:<12} {o.nazwa[:60]}  {'(' + str(o.sztuk) + ' szt.)' if o.sztuk is not None else ''}")
    if wynik.fraza:
        print(f"Fraza z tekstu: „{wynik.fraza}” → {wynik.status_z_tekstu}")
    status, zrodlo = status_produktu(wynik, wybierz_oferte(wynik, ""), config.PROG_MALO_SZTUK)
    print(f"WYNIK: {status} ({zrodlo})")


def test_zamiennikow(id_rekordu: str) -> None:
    produkty = pobierz_produkty(formula=f"RECORD_ID() = '{id_rekordu}'")
    if not produkty:
        print("Nie ma takiego rekordu (albo nie ma linku).")
        return
    dane = modul_zamiennikow.znajdz(produkty[0])
    print(f"Uwagi AI: {dane.get('uwagi')}")
    for z in dane["propozycje"]:
        print(f" ✓ {z['nazwa']} — {z['sklep']} — {zl(z['cena'])} [{z['weryfikacja']}]\n   {z['url']}\n   {z['uzasadnienie']}")
    for o in dane["odrzucone"]:
        print(f" ✗ {o}")


def pobierz_i_odczytaj(url: str):
    """Pobiera stronę i odczytuje produkt: dane ze strony -> API platformy sklepu -> przeglądarka."""
    z_api: dict = {}

    def przegladarka_potrzebna(strona) -> bool:
        if strona.nie_istnieje or not strona.html:
            return False
        if analizuj_html(strona.html, config.PROG_MALO_SZTUK).jest_produkt:
            return False
        z_api["wynik"] = sklepy.z_api(strona.url_koncowy or strona.url, strona.html)
        return z_api["wynik"] is None  # API sklepu wystarczyło — przeglądarka zbędna

    strona = pobierz(url, potrzebna_przegladarka=przegladarka_potrzebna)
    wynik = analizuj_html(strona.html, config.PROG_MALO_SZTUK) if strona.html else None
    if strona.html and not strona.nie_istnieje and (wynik is None or not wynik.jest_produkt):
        zapas = z_api["wynik"] if "wynik" in z_api else sklepy.z_api(strona.url_koncowy or url, strona.html)
        if zapas is not None:
            wynik = zapas
    return strona, wynik


def _cena_sie_zmienila(stare: dict, nowe: dict) -> bool:
    """Znana sprawa „zmiana ceny” wraca jako nowa tylko, gdy cena znowu istotnie się ruszyła."""
    a, b = stare.get("cena"), nowe.get("cena")
    if nowe.get("typ") != ZMIANA_CENY or not a or not b:
        return False
    return istotna_zmiana(a, b)


def przebieg(args) -> int:
    start = time.monotonic()
    czesciowy = bool(args.limit or args.formula)
    stan = Stan.wczytaj()
    produkty = pobierz_produkty(formula=args.formula, limit=args.limit)

    wszystkie: list[Problem] = []
    bledne_ids: set[str] = set()
    czekajace = 0
    strony, problemowe_sklepy = pobierz_wszystkie([p.url for p in produkty])
    os.makedirs(KATALOG_STRON, exist_ok=True)

    for i, p in enumerate(produkty, 1):
        log.info("[%d/%d] %s", i, len(produkty), p.nazwa)
        strona, wynik = strony.get(p.url, (Strona(url=p.url, blad="nie pobrano"), None))
        try:
            problemy, zdjecie = ocen(p, strona, wynik), zdjecie_og(strona.html)
        except Exception as e:  # noqa: BLE001 — jedna dziwna strona nie może zatrzymać całego przebiegu
            log.exception("   Nieoczekiwany błąd przy sprawdzaniu")
            problemy, zdjecie = ocen(p, Strona(url=p.url, blad=f"błąd bota: {type(e).__name__}"), None), ""
        if strona.przez_przegladarke:
            log.info("   (odczytane przez przeglądarkę)")
        if any(x.typ == BLAD for x in problemy) and strona.html and not strona.zablokowana:
            # Strona się wczytała, ale bot jej nie rozumie — zapisujemy do wglądu (artefakt w GitHub Actions).
            with open(os.path.join(KATALOG_STRON, f"{domena(p.url)}__{p.id}.html"), "w", encoding="utf-8") as f:
                f.write(f"<!-- {p.url} -->\n" + strona.html)

        # Chwilowe błędy sklepu: zgłaszamy dopiero po kilku nieudanych próbach z rzędu.
        if any(x.typ == BLAD for x in problemy):
            bledne_ids.add(p.id)
            stan.bledy[p.id] = stan.bledy.get(p.id, 0) + 1
            if stan.bledy[p.id] < config.PROG_BLEDOW:
                for x in problemy:
                    if x.typ == BLAD:
                        log.info("   · nie udało się sprawdzić (%d/%d, zgłoszę po %d z rzędu): %s",
                                 stan.bledy[p.id], config.PROG_BLEDOW, config.PROG_BLEDOW, x.opis)
                problemy = [x for x in problemy if x.typ != BLAD]
        else:
            stan.bledy.pop(p.id, None)

        # Wycofanie: alarm dopiero, gdy produkt wygląda na wycofany w kolejnych przebiegach.
        if any(x.typ in WYMAGA_ZAMIENNIKOW for x in problemy):
            if f"{p.id}:wycofany" not in stan.problemy and not czesciowy:
                stan.podejrzane[p.id] = stan.podejrzane.get(p.id, 0) + 1
                if stan.podejrzane[p.id] < config.PROG_WYCOFANIA:
                    log.info("   … wygląda na wycofany — potwierdzę w kolejnym przebiegu")
                    problemy = [x for x in problemy if x.typ not in WYMAGA_ZAMIENNIKOW]
                    czekajace += 1
        elif p.id not in bledne_ids:
            stan.podejrzane.pop(p.id, None)

        for x in problemy:
            x.zdjecie = zdjecie or p.zdjecie_url
            log.info("   → %s: %s", x.etykieta, x.opis)
        wszystkie += problemy

    if not czesciowy:
        znane_ids = {p.id for p in produkty}
        stan.bledy = {k: v for k, v in stan.bledy.items() if k in znane_ids}
        stan.podejrzane = {k: v for k, v in stan.podejrzane.items() if k in znane_ids}

    # Otwarte sprawy. Produkty, których dziś nie dało się sprawdzić, zachowują poprzednie sprawy
    # (inaczej po chwilowej blokadzie sklepu wróciłyby jako "nowe").
    aktualne = {
        x.klucz: {"typ": x.typ, "id": x.produkt.id, "produkt": x.produkt.nazwa, "cena": x.nowa_cena}
        for x in wszystkie
    }
    for klucz, dane in stan.problemy.items():
        if dane.get("id") in bledne_ids and not klucz.endswith(f":{BLAD}"):
            aktualne.setdefault(klucz, dane)

    if czesciowy:
        nowe_klucze = {x.klucz for x in wszystkie}  # w trybie testowym pokazujemy wszystko
    else:
        nowe_klucze, rozwiazane = stan.porownaj(aktualne, _cena_sie_zmienila)
        if rozwiazane:
            log.info("✓ %d spraw rozwiązanych (poprawione w Airtable albo sklep wrócił do normy).", len(rozwiazane))

    # Zamienniki dla wycofanych — raz na produkt, wynik pamiętany w stanie.
    wycofane_ids = {x.produkt.id for x in wszystkie if x.typ in WYMAGA_ZAMIENNIKOW}
    wycofane_ids |= {d.get("id") for k, d in aktualne.items() if k.endswith(":wycofany")}
    swieze_zamienniki: set[str] = set()
    if not czesciowy:
        stan.wyczysc_zamienniki(wycofane_ids)
    if not czesciowy or args.z_zamiennikami:  # w testach zamienniki tylko na życzenie (zużywają limit Claude)
        swieze_zamienniki = _szukaj_zamiennikow(args, stan, wszystkie, wycofane_ids)

    for x in wszystkie:
        if x.typ in WYMAGA_ZAMIENNIKOW and x.produkt.id in stan.zamienniki:
            x.zamienniki = stan.zamienniki[x.produkt.id]

    # Mail: nowe sprawy (albo świeżo znalezione zamienniki) -> alert; inaczej raz dziennie przypomnienie.
    nowe = [x for x in wszystkie if x.klucz in nowe_klucze or
            (x.typ in WYMAGA_ZAMIENNIKOW and x.produkt.id in swieze_zamienniki)]
    id_nowych = {id(x) for x in nowe}
    pozostale = [x for x in wszystkie if id(x) not in id_nowych]

    id_tab = id_tabeli() if wszystkie else ""
    linki = {x.produkt.id: link_do_rekordu(id_tab, x.produkt.id) for x in wszystkie}
    czas = int(time.monotonic() - start)
    n = len(produkty)
    podsumowanie = (
        f"Sprawdzono {n} {odmiana(n, 'produkt', 'produkty', 'produktów')} w {czas // 60} min {czas % 60} s"
        + (f" · {len(bledne_ids)} nie dało się dziś sprawdzić" if bledne_ids else "")
        + (f" · {czekajace} czeka na potwierdzenie wycofania" if czekajace else "")
        + (" · sklepy blokujące bota: " + ", ".join(f"{d} ({n})" for d, n in problemowe_sklepy.most_common(8))
           if problemowe_sklepy else "")
        + f" · {teraz():%d.%m.%Y, %H:%M}"
    )
    log.info(podsumowanie)

    temat = html = None
    if nowe:
        temat, html = zbuduj(nowe, pozostale, linki, podsumowanie)
    elif (pozostale and config.PRZYPOMNIENIE_CODZIENNE and not stan.mail_dzisiaj()
          and teraz().hour >= config.GODZINA_PRZYPOMNIENIA and not czesciowy):
        temat, html = zbuduj([], pozostale, linki, podsumowanie, przypomnienie=True)

    wyslany = False
    if html:
        if args.podglad:
            with open(args.podglad, "w", encoding="utf-8") as f:
                f.write(html)
            log.info("Podgląd maila zapisany w %s", args.podglad)
        if not args.bez_maila:
            wyslany = wyslij(temat, html)
            if wyslany:
                stan.oznacz_mail()
    else:
        log.info("Brak nowych zmian — mail nie jest potrzebny.")

    if not czesciowy and not args.nie_zapisuj_stanu:
        # Nowe sprawy zapamiętujemy tylko, jeśli mail o nich wyszedł — inaczej wrócą w kolejnym przebiegu.
        stan.zatwierdz(aktualne, nowe_klucze, dostarczone=wyslany or not nowe)
        stan.zapisz()
    if html and not args.bez_maila and not wyslany:
        log.error("Mail nie został wysłany — sprawy zostaną zgłoszone ponownie w kolejnym przebiegu.")
        return 1
    return 0


def _szukaj_zamiennikow(args, stan: Stan, wszystkie: list[Problem], wycofane_ids: set[str]) -> set[str]:
    if not config.ZAMIENNIKI_WLACZONE or args.bez_zamiennikow or not wycofane_ids:
        return set()
    if not modul_zamiennikow.claude_dostepny():
        log.warning("Claude Code niedostępny (brak polecenia `claude` lub CLAUDE_CODE_OAUTH_TOKEN) — pomijam zamienniki.")
        return set()

    def trzeba(x: Problem) -> bool:
        poprzednie = stan.zamienniki.get(x.produkt.id)
        return poprzednie is None or (poprzednie.get("blad") and poprzednie.get("proby", 1) < MAX_PROB_ZAMIENNIKOW)

    kolejka, widziane = [], set()
    for x in wszystkie:
        if x.typ in WYMAGA_ZAMIENNIKOW and x.produkt.id not in widziane and trzeba(x):
            kolejka.append(x)
            widziane.add(x.produkt.id)

    swieze: set[str] = set()
    poczatek = time.monotonic()
    for i, x in enumerate(kolejka):
        if i >= config.MAX_ZAMIENNIKOW_NA_PRZEBIEG or time.monotonic() - poczatek > config.BUDZET_ZAMIENNIKOW_MIN * 60:
            log.info("Pozostałe zamienniki (%d) zostaną wyszukane w kolejnych przebiegach.", len(kolejka) - i)
            break
        poprzednie_proby = (stan.zamienniki.get(x.produkt.id) or {}).get("proby", 0)
        try:
            dane = modul_zamiennikow.znajdz(x.produkt)
        except Exception as e:  # noqa: BLE001
            log.exception("   Błąd przy szukaniu zamienników")
            dane = {"propozycje": [], "uwagi": f"Błąd bota przy szukaniu zamienników ({type(e).__name__}).",
                    "odrzucone": [], "blad": True}
        if dane.get("blad_logowania"):
            # Zły/wygasły token — nie liczymy tego jako próby i nie męczymy kolejnych produktów.
            # Po poprawieniu tokenu zamienniki zostaną wyszukane w następnym przebiegu.
            x.zamienniki = dane
            break
        dane["data"] = teraz().isoformat(timespec="minutes")
        dane["proby"] = poprzednie_proby + 1
        stan.zamienniki[x.produkt.id] = dane
        if not dane.get("blad"):
            swieze.add(x.produkt.id)
    return swieze


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot produktów Interior List")
    parser.add_argument("--url", help="sprawdź jedną stronę i pokaż, co bot z niej odczytuje")
    parser.add_argument("--zamienniki-dla", metavar="RECORD_ID", help="wyszukaj zamienniki dla jednego rekordu")
    parser.add_argument("--limit", type=int, help="sprawdź tylko N produktów (tryb testowy, bez zapisu stanu)")
    parser.add_argument("--formula", help="własna formuła Airtable (tryb testowy, bez zapisu stanu)")
    parser.add_argument("--bez-maila", action="store_true", help="nie wysyłaj maila")
    parser.add_argument("--bez-zamiennikow", action="store_true", help="nie szukaj zamienników")
    parser.add_argument("--z-zamiennikami", action="store_true", help="w trybie testowym też szukaj zamienników")
    parser.add_argument("--podglad", metavar="PLIK.html", help="zapisz treść maila do pliku")
    parser.add_argument("--nie-zapisuj-stanu", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if args.url:
        test_url(args.url)
        return 0
    if not (config.AIRTABLE_TOKEN and config.BASE_ID):
        log.error("Brak AIRTABLE_TOKEN albo BASE_ID.")
        return 2
    if args.zamienniki_dla:
        test_zamiennikow(args.zamienniki_dla)
        return 0
    return przebieg(args)


if __name__ == "__main__":
    sys.exit(main())
