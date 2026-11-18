"""Odczyt produktów z Airtable. Bot NIGDY nie zapisuje do Airtable — ten moduł ma
wyłącznie funkcje czytające. Najlepiej dać botowi token z samym uprawnieniem
`data.records:read`, wtedy zapis jest niemożliwy także technicznie."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pyairtable import Api

from . import config

log = logging.getLogger(__name__)


@dataclass
class Produkt:
    id: str
    nazwa: str
    url: str
    cena: float | None
    wymiar: str
    status: str
    zdjecie_url: str = ""
    zdjecie_pokoju_url: str = ""
    pola: dict = field(default_factory=dict, repr=False)


def _pierwszy_zalacznik(wartosc) -> str:
    if isinstance(wartosc, list) and wartosc and isinstance(wartosc[0], dict):
        # Miniatura "large" jest lżejsza od oryginału, a w zupełności wystarcza AI i do maila.
        miniatury = wartosc[0].get("thumbnails") or {}
        return (miniatury.get("large") or {}).get("url") or wartosc[0].get("url", "")
    return ""


def _tekst(wartosc) -> str:
    if wartosc is None:
        return ""
    if isinstance(wartosc, list):  # pola lookup zwracają listy
        return ", ".join(str(x) for x in wartosc if x is not None)
    return str(wartosc).strip()


def _liczba(wartosc) -> float | None:
    if isinstance(wartosc, list):
        wartosc = wartosc[0] if wartosc else None
    try:
        return float(wartosc) if wartosc not in (None, "") else None
    except (TypeError, ValueError):
        return None


def pobierz_produkty(formula: str | None = None, limit: int | None = None) -> list[Produkt]:
    api = Api(config.AIRTABLE_TOKEN)
    tabela = api.table(config.BASE_ID, config.TABELA_PRODUKTOW)
    parametry = {"formula": formula or config.FORMULA_PRODUKTOW}
    if limit:
        parametry["max_records"] = limit
    rekordy = tabela.all(**parametry)

    produkty = []
    for r in rekordy:
        p = r.get("fields", {})
        url = _tekst(p.get(config.POLE_LINK))
        if not url.startswith("http"):
            continue
        produkty.append(
            Produkt(
                id=r["id"],
                nazwa=_tekst(p.get(config.POLE_NAZWA)) or "Bez nazwy",
                url=url,
                cena=_liczba(p.get(config.POLE_CENA)),
                wymiar=_tekst(p.get(config.POLE_WYMIAR)),
                status=_tekst(p.get(config.POLE_STATUS)) or config.STATUS_DOSTEPNY,
                zdjecie_url=_pierwszy_zalacznik(p.get(config.POLE_ZDJECIE)),
                zdjecie_pokoju_url=_pierwszy_zalacznik(p.get(config.POLE_ZDJECIE_POKOJU)),
                pola=p,
            )
        )
    log.info("Pobrano %d produktów z Airtable.", len(produkty))
    return produkty


def id_tabeli() -> str:
    """ID tabeli do linków w mailu. Z ustawień albo ze schematu bazy (wymaga
    uprawnienia schema.bases:read — jeśli go nie ma, po prostu nie będzie linków)."""
    if config.TABELA_PRODUKTOW_ID:
        return config.TABELA_PRODUKTOW_ID
    if config.TABELA_PRODUKTOW.startswith("tbl"):
        return config.TABELA_PRODUKTOW
    try:
        tabela = Api(config.AIRTABLE_TOKEN).table(config.BASE_ID, config.TABELA_PRODUKTOW)
        return tabela.schema().id
    except Exception as e:  # noqa: BLE001
        log.info("Brak ID tabeli (linki do Airtable w mailu pominięte): %s", e)
        return ""


def link_do_rekordu(id_tab: str, id_rekordu: str) -> str:
    if not id_tab:
        return ""
    return f"https://airtable.com/{config.BASE_ID}/{id_tab}/{id_rekordu}"
