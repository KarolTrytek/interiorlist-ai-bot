"""Odczyt ceny i dostępności ze strony produktu — bez AI.

Kolejność źródeł (od najpewniejszego):
1. JSON-LD schema.org (Product / ProductGroup / Offer) — większość sklepów go ma, bo wymaga go Google,
2. mikrodane (itemprop="price", itemprop="availability"),
3. meta tagi (og:price:amount, product:availability),
4. frazy z widocznego tekstu strony — tylko jako uzupełnienie.

Zasada bezpieczeństwa: fałszywe „wycofany” jest gorsze niż przeoczenie, bo architekt
podmieniłby dobry produkt. Dlatego słabe frazy („chwilowo niedostępny”) liczą się tylko,
gdy strona nie podaje ceny, a tekst nigdy nie nadpisuje dostępności z danych strukturalnych.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

DOSTEPNY = "DOSTEPNY"
MALO = "MALO"
NIEDOSTEPNY = "NIEDOSTEPNY"
NIEZNANY = "NIEZNANY"

# schema.org/ItemAvailability -> nasz status
_MAPA_DOSTEPNOSCI = {
    "instock": DOSTEPNY,
    "onlineonly": DOSTEPNY,
    "instoreonly": DOSTEPNY,
    "preorder": DOSTEPNY,
    "presale": DOSTEPNY,
    "backorder": DOSTEPNY,
    "madetoorder": DOSTEPNY,
    "limitedavailability": MALO,
    "outofstock": NIEDOSTEPNY,
    "soldout": NIEDOSTEPNY,
    "discontinued": NIEDOSTEPNY,
    # warianty spotykane w meta tagach
    "in stock": DOSTEPNY,
    "available": DOSTEPNY,
    "available for order": DOSTEPNY,
    "out of stock": NIEDOSTEPNY,
    "oos": NIEDOSTEPNY,
}

# Mocne frazy: sklep wprost pisze, że produkt zniknął z oferty. Działają także, gdy strona
# wciąż pokazuje starą cenę (np. zakończona oferta Allegro), ale nie wbrew danym strukturalnym.
_FRAZY_WYCOFANY = [
    r"produkt (?:zosta[łl] )?wycofany",
    r"wycofany (?:z oferty|ze sprzeda[żz]y)",
    r"nie jest ju[żz] dost[eę]pny",
    r"produkt nie jest ju[żz] dost[eę]pny",
    r"ta oferta jest zako[ńn]czona",
    r"oferta (?:zosta[łl]a )?zako[ńn]czona",
    r"sprzeda[żz] (?:zosta[łl]a )?zako[ńn]czona",
]
# Słabe frazy: często dotyczą jednego salonu, wariantu albo produktu w karuzeli obok.
# Liczą się tylko wtedy, gdy strona nie podaje ceny ani dostępności.
_FRAZY_NIEDOSTEPNY = [
    r"produkt (?:jest )?(?:obecnie |chwilowo |aktualnie )?niedost[eę]pny",
    r"(?:obecnie|chwilowo|aktualnie|tymczasowo) niedost[eę]pny",
    r"brak (?:produktu |towaru )?w magazynie",
    r"produkt wyprzedany",
    r"produkt nie jest dost[eę]pny",
]

# Strona "nie znaleziono" zwrócona z kodem 200 (tzw. miękkie 404).
_FRAZY_SOFT_404 = [
    r"nie znaleziono strony",
    r"strona nie (?:zosta[łl]a znaleziona|istnieje)",
    r"strona,? kt[oó]rej szukasz,? nie istnieje",
    r"nie mo[żz]emy znale[źz][ćc] (?:tej )?strony",
    r"produkt(?:u)? nie (?:zosta[łl] znaleziony|znaleziono)",
    r"nie znale[źz]li[śs]my (?:tej )?strony",
    r"b[łl][ąa]d 404",
    r"page not found",
]

_FRAZY_MALO = [
    r"ostatnie sztuki",
    r"ostatnia sztuka",
    r"ko[ńn]c[zą]ce si[eę] zapasy",
]

# "zostało 3 szt.", "tylko 5 sztuk", "pozostała 1 sztuka"
_WZOR_SZTUK = re.compile(
    r"(?:zosta[łl](?:o|y|a)?|pozosta[łl](?:o|y|a)?|tylko)"
    r"\s*(\d{1,4})\s*(?:szt\b|szt\.|sztuk|sztuki|sztuka)",
    re.IGNORECASE,
)

# Elementy, których tekst nie dotyczy głównego produktu.
_POMIJANE_TAGI = ["script", "style", "noscript", "svg", "template", "header", "nav", "footer", "aside", "iframe"]
_UKRYTY_STYL = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_POWIAZANE = re.compile(r"related|recommend|similar|carousel|slider|upsell|cross-?sell|polecan|podobn|zobacz-tez", re.I)


@dataclass
class Oferta:
    cena: float | None = None
    status: str = NIEZNANY
    nazwa: str = ""
    sztuk: int | None = None


@dataclass
class WynikStrony:
    oferty: list[Oferta] = field(default_factory=list)
    nazwa_produktu: str = ""
    zrodlo: str = ""
    # status z fraz w tekście (uzupełnienie danych strukturalnych)
    status_z_tekstu: str = NIEZNANY
    fraza_mocna: bool = False
    sztuk_z_tekstu: int | None = None
    fraza: str = ""
    soft_404: bool = False

    @property
    def jest_produkt(self) -> bool:
        """Czy strona wygląda na stronę produktu (znaleźliśmy cenę albo dostępność)."""
        return any(o.cena is not None or o.status != NIEZNANY for o in self.oferty)

    @property
    def ceny(self) -> list[float]:
        return sorted({o.cena for o in self.oferty if o.cena is not None})


# ---------------------------------------------------------------- narzędzia

def parsuj_cene(wartosc) -> float | None:
    """'1 599,00 zł' / '1599.00' / 1599 / '1.599,00' / '1 299 – 1 599 zł' (pierwsza) -> float."""
    if wartosc is None or isinstance(wartosc, bool):
        return None
    if isinstance(wartosc, (int, float)):
        if not math.isfinite(wartosc) or wartosc <= 0:
            return None
        return float(wartosc)
    tekst = re.sub(r"\s+", "", str(wartosc))  # łącznie z twardymi i wąskimi spacjami
    m = re.search(r"\d[\d.,]*", tekst)
    if not m:
        return None
    liczba = m.group(0).rstrip(".,")
    kropki, przecinki = liczba.count("."), liczba.count(",")
    if kropki and przecinki:
        # separator dziesiętny to ten, który występuje później
        if liczba.rfind(",") > liczba.rfind("."):
            liczba = liczba.replace(".", "").replace(",", ".")
        else:
            liczba = liczba.replace(",", "")
    elif przecinki or kropki:
        sep = "," if przecinki else "."
        czesci = liczba.split(sep)
        if len(czesci) > 2:
            liczba = "".join(czesci)  # "1.599.000" -> tysiące
        elif len(czesci[1]) == 3 and 1 <= len(czesci[0]) <= 3:
            liczba = "".join(czesci)  # "1.599" / "1,599" -> tysiąc pięćset...
        else:
            liczba = f"{czesci[0]}.{czesci[1]}"  # "1599,00" / "49,9" / "1599.000"
    try:
        cena = float(liczba)
    except ValueError:
        return None
    return cena if 0 < cena < 10_000_000 else None


def normalizuj_dostepnosc(wartosc) -> str:
    if not wartosc:
        return NIEZNANY
    if isinstance(wartosc, list):
        wartosc = wartosc[0] if wartosc else ""
    if isinstance(wartosc, dict):
        wartosc = wartosc.get("@id") or wartosc.get("name") or ""
    tekst = str(wartosc).strip()
    tekst = re.sub(r"^https?://schema\.org/", "", tekst, flags=re.IGNORECASE)
    klucz = tekst.lower().replace("_", " ")
    if klucz in _MAPA_DOSTEPNOSCI:
        return _MAPA_DOSTEPNOSCI[klucz]
    return _MAPA_DOSTEPNOSCI.get(klucz.replace(" ", ""), NIEZNANY)


def _jako_lista(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _typy(obiekt: dict) -> set[str]:
    return {str(t).split("/")[-1].lower() for t in _jako_lista(obiekt.get("@type"))}


def _int_lub_none(x) -> int | None:
    try:
        if isinstance(x, dict):
            x = x.get("value")
        liczba = float(x)
        return int(liczba) if math.isfinite(liczba) and liczba >= 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- JSON-LD

def _wszystkie_obiekty(dane):
    """Rekurencyjnie zwraca każdy słownik z drzewa JSON-LD (obsługuje @graph i listy)."""
    if isinstance(dane, dict):
        yield dane
        for wartosc in dane.values():
            if isinstance(wartosc, (dict, list)):
                yield from _wszystkie_obiekty(wartosc)
    elif isinstance(dane, list):
        for element in dane:
            yield from _wszystkie_obiekty(element)


def _oferty_z_offers(offers, nazwa_wariantu: str = "") -> list[Oferta]:
    wynik: list[Oferta] = []
    for oferta in _jako_lista(offers):
        if not isinstance(oferta, dict):
            continue
        if "aggregateoffer" in _typy(oferta):
            zagniezdzone = _oferty_z_offers(oferta.get("offers"), nazwa_wariantu)
            if zagniezdzone:
                wynik.extend(zagniezdzone)
                continue
            # Zakres cen bez wariantów: bez nazwy, żeby nie dało się go "dopasować" po wymiarze.
            status = normalizuj_dostepnosc(oferta.get("availability"))
            for klucz in ("lowPrice", "highPrice", "price"):
                cena = parsuj_cene(oferta.get(klucz))
                if cena is not None:
                    wynik.append(Oferta(cena=cena, status=status, nazwa=""))
            continue
        cena = parsuj_cene(oferta.get("price"))
        if cena is None:
            for s in _jako_lista(oferta.get("priceSpecification")):
                if isinstance(s, dict):
                    cena = parsuj_cene(s.get("price"))
                    if cena is not None:
                        break
        wynik.append(
            Oferta(
                cena=cena,
                status=normalizuj_dostepnosc(oferta.get("availability")),
                nazwa=str(oferta.get("name") or nazwa_wariantu or ""),
                sztuk=_int_lub_none(oferta.get("inventoryLevel")),
            )
        )
    return wynik


def _wczytaj_json(surowe: str):
    try:
        return json.loads(surowe)
    except (json.JSONDecodeError, ValueError):
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", surowe))
        except (json.JSONDecodeError, ValueError):
            return None


def _z_json_ld(soup: BeautifulSoup) -> tuple[list[Oferta], str]:
    oferty: list[Oferta] = []
    nazwa = ""
    for skrypt in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        surowe = (skrypt.string or skrypt.get_text() or "").strip()
        dane = _wczytaj_json(surowe) if surowe else None
        if dane is None:
            continue
        for obiekt in _wszystkie_obiekty(dane):
            typy = _typy(obiekt)
            if "product" in typy or "productgroup" in typy:
                if not nazwa and obiekt.get("name"):
                    nazwa = str(obiekt.get("name"))
                if obiekt.get("offers") is not None:
                    oferty.extend(_oferty_z_offers(obiekt.get("offers"), str(obiekt.get("name") or "")))
    return oferty, nazwa


# ---------------------------------------------------------------- mikrodane i meta

def _z_mikrodanych(soup: BeautifulSoup) -> list[Oferta]:
    ceny = []
    for el in soup.find_all(attrs={"itemprop": "price"}):
        cena = parsuj_cene(el.get("content") or el.get_text())
        if cena is not None:
            ceny.append(cena)
    status = NIEZNANY
    for el in soup.find_all(attrs={"itemprop": "availability"}):
        status = normalizuj_dostepnosc(el.get("href") or el.get("content") or el.get_text())
        if status != NIEZNANY:
            break
    if not ceny and status == NIEZNANY:
        return []
    if not ceny:
        return [Oferta(status=status)]
    return [Oferta(cena=c, status=status) for c in sorted(set(ceny))]


def _meta(soup: BeautifulSoup, *nazwy: str) -> str:
    for n in nazwy:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if el and el.get("content"):
            return el["content"]
    return ""


def _z_meta(soup: BeautifulSoup) -> list[Oferta]:
    cena = parsuj_cene(_meta(soup, "product:price:amount", "og:price:amount", "product:sale_price:amount"))
    status = normalizuj_dostepnosc(_meta(soup, "product:availability", "og:availability"))
    if cena is None and status == NIEZNANY:
        return []
    return [Oferta(cena=cena, status=status)]


# ---------------------------------------------------------------- tekst

def tekst_strony(soup: BeautifulSoup) -> str:
    """Widoczny tekst głównej części strony — bez skryptów, menu, stopki,
    ukrytych elementów i karuzel z innymi produktami."""
    kopia = BeautifulSoup(str(soup), "lxml")
    for el in kopia(_POMIJANE_TAGI):
        el.decompose()
    for el in list(kopia.find_all(True)):
        if getattr(el, "decomposed", False):
            continue
        atrybuty = el.attrs or {}
        klasa = atrybuty.get("class", "")
        klasy = " ".join(klasa) if isinstance(klasa, list) else str(klasa)
        if (
            "hidden" in atrybuty
            or atrybuty.get("aria-hidden") == "true"
            or _UKRYTY_STYL.search(str(atrybuty.get("style", "")))
            or _POWIAZANE.search(klasy + " " + str(atrybuty.get("id", "")))
        ):
            el.decompose()
    return re.sub(r"\s+", " ", kopia.get_text(" ", strip=True))


def _szukaj(wzory: list[str], tekst: str):
    for wzor in wzory:
        m = re.search(wzor, tekst)
        if m:
            return m
    return None


def _frazy(tekst: str, wynik: WynikStrony, prog_malo: int) -> None:
    maly = tekst.lower()
    m = _szukaj(_FRAZY_WYCOFANY, maly)
    if m:
        wynik.status_z_tekstu, wynik.fraza_mocna, wynik.fraza = NIEDOSTEPNY, True, m.group(0)
        return
    m = _szukaj(_FRAZY_NIEDOSTEPNY, maly)
    if m:
        wynik.status_z_tekstu, wynik.fraza = NIEDOSTEPNY, m.group(0)
        return
    m = _szukaj(_FRAZY_MALO, maly)
    if m:
        wynik.status_z_tekstu, wynik.fraza = MALO, m.group(0)
        return
    liczby = {int(x.group(1)) for x in _WZOR_SZTUK.finditer(maly)}
    # Kilka różnych liczb to zwykle stany w poszczególnych salonach — wtedy nic nie wnioskujemy.
    if len(liczby) == 1:
        n = liczby.pop()
        wynik.sztuk_z_tekstu = n
        if 0 < n < prog_malo:
            wynik.status_z_tekstu, wynik.fraza = MALO, f"zostało {n} szt."


# ---------------------------------------------------------------- główna funkcja

def analizuj_html(html: str, prog_malo: int = 20) -> WynikStrony:
    soup = BeautifulSoup(html or "", "lxml")
    wynik = WynikStrony()

    oferty, nazwa = _z_json_ld(soup)
    if oferty:
        wynik.oferty, wynik.zrodlo = oferty, "dane strukturalne (JSON-LD)"
    else:
        oferty = _z_mikrodanych(soup)
        if oferty:
            wynik.oferty, wynik.zrodlo = oferty, "mikrodane"
        else:
            oferty = _z_meta(soup)
            if oferty:
                wynik.oferty, wynik.zrodlo = oferty, "meta tagi"
    wynik.nazwa_produktu = nazwa

    # JSON-LD bez dostępności -> spróbuj z mikrodanych lub meta.
    if wynik.oferty and all(o.status == NIEZNANY for o in wynik.oferty):
        for zapas in (_z_mikrodanych(soup), _z_meta(soup)):
            statusy = [o.status for o in zapas if o.status != NIEZNANY]
            if statusy:
                for o in wynik.oferty:
                    o.status = statusy[0]
                break

    tekst = tekst_strony(soup)
    _frazy(tekst, wynik, prog_malo)

    if not wynik.jest_produkt:
        tytul = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
        poczatek = tekst[:4000].lower()
        if re.search(r"(?<!\d)404(?!\d)", tytul) or _szukaj(_FRAZY_SOFT_404, tytul + " " + poczatek):
            wynik.soft_404 = True
    return wynik


def zdjecie_og(html: str) -> str:
    """Zdjęcie produktu ze strony sklepu (og:image) — stały adres, w przeciwieństwie
    do linków do załączników Airtable, które wygasają po kilku godzinach."""
    m = re.search(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]*>', html or "", re.I)
    if not m:
        return ""
    c = re.search(r'content=["\']([^"\']+)["\']', m.group(0), re.I)
    url = c.group(1).strip() if c else ""
    return url if url.startswith("https://") else ""


def wybierz_oferte(wynik: WynikStrony, wymiar: str) -> Oferta | None:
    """Gdy produkt ma warianty, wybiera wariant pasujący do wymiaru z Airtable — ale tylko
    jeśli pasuje dokładnie jeden wariant i zgadzają się wszystkie liczby. Inaczej None
    (wtedy oceniamy wszystkie warianty łącznie, co jest bezpieczniejsze)."""
    if not wynik.oferty:
        return None
    if len(wynik.oferty) == 1:
        return wynik.oferty[0]
    liczby = set(re.findall(r"\d{2,4}", wymiar or ""))
    if not liczby:
        return None
    pasujace = [
        o for o in wynik.oferty
        if o.nazwa and all(re.search(rf"(?<!\d){n}(?!\d)", o.nazwa) for n in liczby)
    ]
    return pasujace[0] if len(pasujace) == 1 else None


def status_produktu(wynik: WynikStrony, oferta: Oferta | None, prog_malo: int = 20) -> tuple[str, str]:
    """Łączy dane strukturalne i frazy z tekstu w jeden status + opis źródła."""
    if oferta is not None and oferta.status != NIEZNANY:
        status, zrodlo = oferta.status, wynik.zrodlo
        # Stan magazynu tylko podbija do „mało sztuk” — nigdy sam nie oznacza wycofania
        # (sklep z „InStock” i 0 szt. zwykle realizuje zamówienia z opóźnieniem).
        if status == DOSTEPNY and oferta.sztuk is not None and oferta.sztuk < prog_malo:
            return MALO, f"{wynik.zrodlo}: {oferta.sztuk} szt."
    elif oferta is not None and oferta.sztuk == 0 and oferta.cena is None:
        return NIEDOSTEPNY, f"{wynik.zrodlo}: 0 szt."
    else:
        statusy = {o.status for o in (wynik.oferty if oferta is None else [oferta]) if o.status != NIEZNANY}
        if DOSTEPNY in statusy:
            status = DOSTEPNY
        elif MALO in statusy:
            status = MALO
        elif statusy == {NIEDOSTEPNY}:
            status = NIEDOSTEPNY
        else:
            status = NIEZNANY
        zrodlo = wynik.zrodlo

    # Dostępny wg danych, ale tekst mówi „ostatnie sztuki” -> mało sztuk.
    if status == DOSTEPNY and wynik.status_z_tekstu == MALO:
        return MALO, f"strona: „{wynik.fraza}”"
    if status == NIEZNANY:
        # Mocna fraza („oferta zakończona”) wystarcza nawet przy cenie na stronie;
        # słaba („chwilowo niedostępny”) tylko wtedy, gdy strona nie podaje ceny.
        if wynik.status_z_tekstu == NIEDOSTEPNY and (wynik.fraza_mocna or not wynik.ceny):
            return NIEDOSTEPNY, f"strona: „{wynik.fraza}”"
        if wynik.status_z_tekstu == MALO:
            return MALO, f"strona: „{wynik.fraza}”"
        if wynik.ceny:
            # Cena bez informacji o braku -> da się kupić (tak samo zakładał stary bot).
            return DOSTEPNY, f"{zrodlo} (cena bez informacji o braku)"
    return status, zrodlo
