"""Szukanie zamienników dla wycofanych produktów przez Claude Code (`claude -p`)
na Twojej subskrypcji Claude (token z `claude setup-token` w CLAUDE_CODE_OAUTH_TOKEN).

Zabezpieczenia przed zmyślonymi linkami (problem starego bota):
- Claude może otwierać strony (WebFetch) tylko w sklepach z listy — reszta jest blokowana uprawnieniami,
- odpowiedź ma sztywny format JSON (--json-schema), więc nie ma czego "parsować z HTML-a",
- każdy zaproponowany link sprawdzamy potem SAMI w Pythonie: domena z listy, strona istnieje,
  to strona produktu i produkt jest dostępny. Jeśli sklep blokuje nasze sprawdzenie, akceptujemy link
  tylko wtedy, gdy Claude naprawdę otworzył tę stronę w trakcie pracy (widać to w zapisie sesji).
"""

from __future__ import annotations

import json
import logging
import re
import mimetypes
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from . import config
from .airtable_odczyt import Produkt
from .ekstrakcja import NIEDOSTEPNY, NIEZNANY, analizuj_html, status_produktu, wybierz_oferte, zdjecie_og
from .porownanie import zl
from .strona import domena, pobierz, zamknij_przegladarke

log = logging.getLogger(__name__)

KATALOG_PROMPTOW = Path(__file__).resolve().parent.parent / "prompty"

SCHEMAT = {
    "type": "object",
    "properties": {
        "propozycje": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nazwa": {"type": "string"},
                    "sklep": {"type": "string"},
                    "url": {"type": "string"},
                    "cena_pln": {"type": ["number", "null"]},
                    "wymiary": {"type": "string"},
                    "uzasadnienie": {"type": "string"},
                },
                "required": ["nazwa", "sklep", "url", "cena_pln", "wymiary", "uzasadnienie"],
                "additionalProperties": False,
            },
        },
        "uwagi": {"type": "string"},
    },
    "required": ["propozycje", "uwagi"],
    "additionalProperties": False,
}

_PARAMETRY_SLEDZACE = ("utm_", "gclid", "fbclid", "srsltid", "ref", "aff", "_gl")


@dataclass
class Zamiennik:
    nazwa: str
    sklep: str
    url: str
    cena: float | None
    cena_ze_strony: bool
    wymiary: str
    uzasadnienie: str
    zdjecie: str = ""
    weryfikacja: str = ""  # "sprawdzony" albo "otwarty przez AI"

    def jako_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- dozwolone sklepy

def dozwolony_sklep(url: str) -> str | None:
    """Zwraca nazwę sklepu, jeśli adres należy do sklepu z listy; inaczej None."""
    try:
        czesci = urlparse(url)
    except ValueError:
        return None
    if czesci.scheme not in ("http", "https"):
        return None
    host = domena(url)
    sciezka = czesci.path or "/"
    for wpis, nazwa in config.SKLEPY.items():
        dom, _, prefiks = wpis.partition("/")
        if host == dom or host.endswith("." + dom):
            p = "/" + prefiks.lower()
            if not prefiks or sciezka.lower() == p or sciezka.lower().startswith(p + "/"):
                return nazwa
    return None


def reguly_webfetch() -> list[str]:
    reguly = []
    for wpis in config.SKLEPY:
        dom = wpis.split("/")[0]
        reguly += [f"WebFetch(domain:{dom})", f"WebFetch(domain:www.{dom})"]
    return reguly


def normalizuj_url(url: str) -> str:
    try:
        c = urlparse(url.strip())
    except ValueError:
        return url.strip()
    zapytanie = [(k, v) for k, v in parse_qsl(c.query) if not k.lower().startswith(_PARAMETRY_SLEDZACE)]
    return urlunparse(
        ("https", domena(url), c.path.rstrip("/") or "/", "", urlencode(zapytanie), "")
    )


# ---------------------------------------------------------------- przygotowanie

def _pobierz_zdjecie(url: str, katalog: Path, nazwa: str) -> str:
    if not url:
        return ""
    try:
        odp = requests.get(url, timeout=30)
        odp.raise_for_status()
        typ = (odp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        rozszerzenie = mimetypes.guess_extension(typ) or ".jpg"
        if rozszerzenie == ".jpe":
            rozszerzenie = ".jpg"
        plik = katalog / f"{nazwa}{rozszerzenie}"
        plik.write_bytes(odp.content)
        return plik.name
    except requests.RequestException as e:
        log.warning("   Nie udało się pobrać zdjęcia %s: %s", nazwa, e)
        return ""


def zbuduj_prompt(p: Produkt, plik_produktu: str, plik_pokoju: str) -> str:
    szablon = (KATALOG_PROMPTOW / "zamienniki.md").read_text(encoding="utf-8")
    zdjecia = []
    if plik_produktu:
        zdjecia.append(f"- `{plik_produktu}` — zdjęcie wycofanego produktu")
    if plik_pokoju:
        zdjecia.append(f"- `{plik_pokoju}` — wizualizacja pomieszczenia, w którym stoi ten produkt")
    opis_zdjec = (
        "## Zdjęcia (w bieżącym katalogu)\n\n" + "\n".join(zdjecia)
        if zdjecia
        else "## Zdjęcia\n\nBrak zdjęć — opieraj się na nazwie, wymiarach i starym linku."
    )
    sklepy = "\n".join(f"- {wpis}" for wpis in config.SKLEPY)
    return szablon.format(
        nazwa=p.nazwa,
        wymiar=p.wymiar or "brak danych",
        cena=zl(p.cena) if p.cena else "brak danych",
        stary_url=p.url,
        opis_zdjec=opis_zdjec,
        sklepy=sklepy,
    )


# ---------------------------------------------------------------- Claude Code

def claude_dostepny() -> bool:
    return shutil.which("claude") is not None and bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY"))


def parsuj_strumien(linie: list[str]) -> tuple[dict | None, set[str], dict]:
    """Z wyjścia `--output-format stream-json` wyciąga: wynik strukturalny,
    zbiór adresów, które Claude faktycznie otworzył (WebFetch bez błędu), i metadane."""
    wywolania: dict[str, str] = {}  # tool_use_id -> url
    otwarte: set[str] = set()
    wynik: dict | None = None
    meta: dict = {}
    for linia in linie:
        linia = linia.strip()
        if not linia.startswith("{"):
            continue
        try:
            zdarzenie = json.loads(linia)
        except json.JSONDecodeError:
            continue
        typ = zdarzenie.get("type")
        if typ in ("assistant", "user"):
            for blok in (zdarzenie.get("message") or {}).get("content") or []:
                if not isinstance(blok, dict):
                    continue
                if blok.get("type") == "tool_use" and blok.get("name") == "WebFetch":
                    url = (blok.get("input") or {}).get("url")
                    if url:
                        wywolania[blok.get("id", "")] = url
                elif blok.get("type") == "tool_result" and blok.get("tool_use_id") in wywolania:
                    if not blok.get("is_error"):
                        otwarte.add(normalizuj_url(wywolania[blok["tool_use_id"]]))
        elif typ == "result":
            meta = {k: zdarzenie.get(k) for k in ("subtype", "is_error", "num_turns", "total_cost_usd", "result")}
            so = zdarzenie.get("structured_output")
            if isinstance(so, dict):
                wynik = so
            elif isinstance(zdarzenie.get("result"), str):
                try:
                    kandydat = json.loads(zdarzenie["result"])
                    if isinstance(kandydat, dict) and "propozycje" in kandydat:
                        wynik = kandydat
                except json.JSONDecodeError:
                    pass
    return wynik, otwarte, meta


_TAJNE = re.compile(r"AIRTABLE|GMAIL|PASSWORD|PASSWD|SECRET|GITHUB_TOKEN|ACTIONS_|_TOKEN$|_KEY$", re.I)
_POTRZEBNE_CLAUDE = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}


def _srodowisko_dla_claude() -> dict:
    """Claude dostaje tylko swój token — bez haseł do Airtable i Gmaila."""
    env = {k: v for k, v in os.environ.items() if k in _POTRZEBNE_CLAUDE or not _TAJNE.search(k)}
    # Token skopiowany z terminala często ma złamania linii albo spacje — wtedy API zwraca 401.
    for k in _POTRZEBNE_CLAUDE:
        if env.get(k):
            env[k] = re.sub(r"\s+", "", env[k])
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def opis_tokenu() -> str:
    """Diagnostyka bez ujawniania tokenu: czy wygląda poprawnie i ile ma znaków."""
    token = re.sub(r"\s+", "", os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or "")
    if not token:
        return "brak tokenu CLAUDE_CODE_OAUTH_TOKEN"
    surowy = os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or ""
    uwagi = []
    if not token.startswith("sk-ant-oat"):
        uwagi.append("nie zaczyna się od sk-ant-oat01- — to chyba nie token z `claude setup-token`")
    if surowy != surowy.strip() or re.search(r"\s", surowy.strip()):
        uwagi.append("zawierał spacje/złamania linii (usunięte automatycznie)")
    return f"token: {len(token)} znaków" + (" — " + "; ".join(uwagi) if uwagi else ", format OK")


_BLEDY_LOGOWANIA = re.compile(r"401|invalid bearer|failed to authenticate|authentication|unauthori|oauth", re.I)


def uruchom_claude(prompt: str, katalog: Path) -> tuple[dict | None, set[str], dict]:
    dozwolone = ",".join(["WebSearch", "Read", *reguly_webfetch()])
    polecenie = [
        "claude",
        "-p",
        prompt,
        "--model", config.MODEL_ZAMIENNIKOW,
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(SCHEMAT, ensure_ascii=False),
        "--tools", "WebSearch,WebFetch,Read",
        "--allowedTools", dozwolone,
        "--permission-mode", "dontAsk",
        "--max-turns", str(config.MAX_TUR_ZAMIENNIKOW),
        "--no-session-persistence",
    ]
    srodowisko = _srodowisko_dla_claude()
    try:
        proces = subprocess.run(
            polecenie,
            cwd=katalog,
            env=srodowisko,
            capture_output=True,
            text=True,
            timeout=config.TIMEOUT_ZAMIENNIKOW_S,
        )
    except subprocess.TimeoutExpired as e:
        wyjscie = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        wynik, otwarte, meta = parsuj_strumien(wyjscie.splitlines())
        meta["blad"] = f"przekroczono czas {config.TIMEOUT_ZAMIENNIKOW_S} s"
        return wynik, otwarte, meta
    wynik, otwarte, meta = parsuj_strumien(proces.stdout.splitlines())
    if proces.returncode != 0 and not wynik:
        meta["blad"] = (proces.stderr or meta.get("result") or f"kod wyjścia {proces.returncode}")[:500]
    return wynik, otwarte, meta


# ---------------------------------------------------------------- weryfikacja

def weryfikuj(propozycje: list[dict], otwarte_przez_ai: set[str]) -> tuple[list[Zamiennik], list[str]]:
    zaakceptowane: list[Zamiennik] = []
    odrzucone: list[str] = []
    widziane: set[str] = set()
    for prop in propozycje[:3]:
        url = (prop.get("url") or "").strip()
        sklep = dozwolony_sklep(url)
        if not sklep:
            odrzucone.append(f"{prop.get('nazwa')}: sklep spoza listy ({domena(url) or 'brak linku'})")
            continue
        klucz = normalizuj_url(url)
        if klucz in widziane:
            continue
        widziane.add(klucz)

        strona = pobierz(url, potrzebna_przegladarka=lambda s: not analizuj_html(s.html).jest_produkt)
        z = Zamiennik(
            nazwa=(prop.get("nazwa") or "").strip(),
            sklep=sklep,
            url=url,
            cena=prop.get("cena_pln"),
            cena_ze_strony=False,
            wymiary=(prop.get("wymiary") or "").strip(),
            uzasadnienie=(prop.get("uzasadnienie") or "").strip(),
        )

        if strona.nie_istnieje:
            odrzucone.append(f"{z.nazwa}: strona nie istnieje ({strona.kod})")
            continue
        wynik = analizuj_html(strona.html) if strona.html else None
        if wynik is not None and wynik.jest_produkt:
            oferta = wybierz_oferte(wynik, z.wymiary)
            status, _ = status_produktu(wynik, oferta)
            if status == NIEDOSTEPNY:
                odrzucone.append(f"{z.nazwa}: sklep pokazuje, że jest niedostępny")
                continue
            cena = (oferta.cena if oferta and oferta.cena else None) or (wynik.ceny[0] if wynik.ceny else None)
            if cena:
                z.cena, z.cena_ze_strony = cena, True
            z.zdjecie = zdjecie_og(strona.html)
            z.weryfikacja = "sprawdzony" if status != NIEZNANY else "strona produktu istnieje"
            zaakceptowane.append(z)
        elif wynik is not None and (wynik.soft_404 or strona.na_strone_glowna):
            odrzucone.append(f"{z.nazwa}: link prowadzi do strony „nie znaleziono” albo strony głównej")
        elif klucz in otwarte_przez_ai:
            # Nasze sprawdzenie zablokowane, ale Claude otworzył tę stronę w trakcie pracy.
            z.weryfikacja = "otwarty przez AI"
            z.zdjecie = zdjecie_og(strona.html) if strona.html else ""
            zaakceptowane.append(z)
        else:
            odrzucone.append(f"{z.nazwa}: nie udało się potwierdzić, że link prowadzi do produktu")
    return zaakceptowane, odrzucone


# ---------------------------------------------------------------- całość

def znajdz(p: Produkt) -> dict:
    """Zwraca słownik gotowy do zapisania w stanie i pokazania w mailu."""
    with tempfile.TemporaryDirectory(prefix="zamienniki_") as tmp:
        katalog = Path(tmp)
        plik_produktu = _pobierz_zdjecie(p.zdjecie_url, katalog, "produkt")
        plik_pokoju = _pobierz_zdjecie(p.zdjecie_pokoju_url, katalog, "pokoj")
        prompt = zbuduj_prompt(p, plik_produktu, plik_pokoju)
        log.info("   🤖 Claude szuka zamienników dla: %s", p.nazwa)
        wynik, otwarte, meta = uruchom_claude(prompt, katalog)

    if not wynik:
        zamknij_przegladarke()
        powod = meta.get("blad") or meta.get("result") or "brak odpowiedzi"
        log.warning("   Claude nie zwrócił propozycji: %s", str(powod)[:300])
        blad_logowania = bool(_BLEDY_LOGOWANIA.search(str(powod)))
        if blad_logowania:
            log.warning("   Problem z tokenem Claude (%s). Wygeneruj nowy: `claude setup-token`.", opis_tokenu())
        return {"propozycje": [], "uwagi": f"Nie udało się wygenerować zamienników ({str(powod)[:200]}).",
                "odrzucone": [], "blad": True, "blad_logowania": blad_logowania}

    try:
        zaakceptowane, odrzucone = weryfikuj(wynik.get("propozycje") or [], otwarte)
    finally:
        zamknij_przegladarke()
    log.info("   ✓ %d zamienników potwierdzonych, %d odrzuconych", len(zaakceptowane), len(odrzucone))
    return {
        "propozycje": [z.jako_dict() for z in zaakceptowane],
        "uwagi": (wynik.get("uwagi") or "").strip(),
        "odrzucone": odrzucone,
        "blad": False,
    }
