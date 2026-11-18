"""Odczyt przez publiczne API platform sklepowych — dla sklepów, które nie publikują danych
produktu na stronie (np. WooCommerce z własnym szablonem w Elementorze).

WooCommerce ma wbudowane, publiczne Store API (`/wp-json/wc/store/v1/products?slug=…`) —
to te same dane, z których korzysta koszyk sklepu: cena, cena regularna, stan magazynu."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from . import config
from .ekstrakcja import DOSTEPNY, MALO, NIEDOSTEPNY, NIEZNANY, Oferta, WynikStrony
from .strona import _pobierz_sesje

log = logging.getLogger(__name__)

_SCIEZKA_WOO = re.compile(r"/(?:product|produkt|sklep/produkt)/([^/?#]+)/?", re.I)


def _slug_woocommerce(url: str, html: str) -> str:
    if html and "woocommerce" not in html.lower() and "wp-content" not in html.lower():
        return ""
    m = _SCIEZKA_WOO.search(urlparse(url).path)
    return m.group(1) if m else ""


def _cena(ceny: dict, klucz: str) -> float | None:
    try:
        wartosc = ceny.get(klucz)
        if wartosc in (None, ""):
            return None
        jednostka = int(ceny.get("currency_minor_unit", 2))
        cena = int(wartosc) / (10 ** jednostka)
        return cena if cena > 0 else None
    except (TypeError, ValueError):
        return None


def wynik_z_woocommerce(produkt: dict, prog_malo: int) -> WynikStrony | None:
    ceny = produkt.get("prices") or {}
    cena = _cena(ceny, "price") or _cena(ceny, "sale_price") or _cena(ceny, "regular_price")
    if produkt.get("is_in_stock") is False:
        status = NIEDOSTEPNY
    elif produkt.get("is_in_stock") is True:
        status = DOSTEPNY
    else:
        status = NIEZNANY
    klasa = ((produkt.get("stock_availability") or {}).get("class") or "").lower()
    if "out-of-stock" in klasa:
        status = NIEDOSTEPNY
    sztuk = produkt.get("low_stock_remaining")
    if status == DOSTEPNY and isinstance(sztuk, int) and 0 < sztuk < prog_malo:
        status = MALO
    if cena is None and status == NIEZNANY:
        return None
    wynik = WynikStrony(zrodlo="API sklepu (WooCommerce)", nazwa_produktu=str(produkt.get("name") or ""))
    wynik.oferty = [Oferta(cena=cena, status=status, nazwa=wynik.nazwa_produktu,
                           sztuk=sztuk if isinstance(sztuk, int) else None)]
    return wynik


def z_api(url: str, html: str) -> WynikStrony | None:
    """Próbuje odczytać produkt z API platformy. None, gdy to nie ta platforma albo API nie odpowiada."""
    slug = _slug_woocommerce(url, html)
    if not slug:
        return None
    czesci = urlparse(url)
    adres = f"{czesci.scheme}://{czesci.netloc}/wp-json/wc/store/v1/products"
    try:
        odp = _pobierz_sesje().get(adres, params={"slug": slug}, timeout=config.TIMEOUT_S,
                                   headers={"Accept": "application/json"})
        if odp.status_code != 200:
            return None
        dane = odp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(dane, list) or not dane:
        return None
    wynik = wynik_z_woocommerce(dane[0], config.PROG_MALO_SZTUK)
    if wynik:
        log.info("   ✓ odczytano z API sklepu (WooCommerce)")
    return wynik
