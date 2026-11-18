"""Mail z raportem — w stylistyce Interior List (Helvetica Neue, burgund #520005,
beżowe boksy #f5f2ef). Układ na tabelach, żeby dobrze wyglądał też w Outlooku."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

from . import config
from .porownanie import (
    BLAD,
    KOLEJNOSC,
    MALO_SZTUK,
    NIEDOST,
    WROCIL,
    ZMIANA_CENY,
    ZNIKNAL,
    Problem,
    zl,
)

log = logging.getLogger(__name__)

BURGUND = "#520005"
BEZ = "#f5f2ef"
TEKST = "#1a1a1a"
SZARY = "#525252"
ETYKIETA = "#9a9a9a"
LINIA = "#e8e0d8"
FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"

_KOLORY = {
    ZNIKNAL: (BURGUND, "#ffffff"),
    NIEDOST: (BURGUND, "#ffffff"),
    MALO_SZTUK: ("#f3e3c3", "#6b4200"),
    ZMIANA_CENY: ("#e8e0d8", TEKST),
    WROCIL: ("#dfeadf", "#24502c"),
    BLAD: ("#ececec", SZARY),
}


def odmiana(n: int, jeden: str, kilka: str, wiele: str) -> str:
    if n == 1:
        return jeden
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return kilka
    return wiele


def _e(x) -> str:
    return escape(str(x or ""), quote=True)


def _plakietka(p: Problem) -> str:
    tlo, kolor = _KOLORY[p.typ]
    return (
        f'<span style="display:inline-block;background:{tlo};color:{kolor};font-size:11px;font-weight:600;'
        f'letter-spacing:0.08em;text-transform:uppercase;padding:4px 10px;border-radius:3px;">{_e(p.etykieta)}</span>'
    )


def _link(url: str, tekst: str, kolor: str = BURGUND) -> str:
    if not url:
        return ""
    return (
        f'<a href="{_e(url)}" style="color:{kolor};font-weight:600;text-decoration:none;font-size:13px;">'
        f"{_e(tekst)}&nbsp;→</a>"
    )


def _wiersz(etykieta: str, wartosc: str, ostatni: bool = False) -> str:
    linia = "" if ostatni else f"border-bottom:1px solid {LINIA};"
    return (
        f'<tr><td style="padding:7px 0;{linia}font-size:11px;letter-spacing:0.08em;text-transform:uppercase;'
        f'color:{ETYKIETA};width:110px;vertical-align:top;">{_e(etykieta)}</td>'
        f'<td style="padding:7px 0;{linia}font-size:14px;color:{TEKST};vertical-align:top;">{wartosc}</td></tr>'
    )


def _zamienniki_html(dane: dict) -> str:
    if not dane:
        return ""
    propozycje = dane.get("propozycje") or []
    czesci = [
        f'<tr><td colspan="2" style="padding-top:18px;">'
        f'<div style="font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:{BURGUND};font-weight:600;'
        f'padding-bottom:8px;">Propozycje zamienników</div>'
    ]
    if not propozycje:
        uwagi = dane.get("uwagi") or "Nie znaleziono pewnych zamienników w sklepach z listy."
        czesci.append(f'<div style="font-size:13px;color:{SZARY};line-height:1.6;">{_e(uwagi)}</div>')
    for z in propozycje:
        cena = zl(z.get("cena")) if z.get("cena") else "cena na stronie"
        if z.get("cena") and not z.get("cena_ze_strony"):
            cena += " (wg AI)"
        miniatura = (
            f'<td width="64" style="padding:12px;vertical-align:top;">'
            f'<img src="{_e(z["zdjecie"])}" width="64" height="64" alt="" '
            f'style="display:block;width:64px;height:64px;object-fit:cover;border-radius:4px;"></td>'
            if z.get("zdjecie")
            else ""
        )
        lewy_odstep = "0" if miniatura else "16px"
        znacznik = (
            f'<span style="font-size:11px;color:{ETYKIETA};"> · link otwarty przez AI, sklep blokuje nasze sprawdzenie</span>'
            if z.get("weryfikacja") == "otwarty przez AI"
            else ""
        )
        czesci.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:#ffffff;border-radius:6px;margin-bottom:8px;"><tr>'
            f'{miniatura}'
            f'<td style="padding:12px 12px 12px {lewy_odstep};vertical-align:top;">'
            f'<div style="font-size:14px;font-weight:600;color:{TEKST};">{_e(z.get("nazwa"))}</div>'
            f'<div style="font-size:12px;color:{SZARY};padding:2px 0 6px;">{_e(z.get("sklep"))} · {_e(cena)}'
            f'{" · " + _e(z.get("wymiary")) if z.get("wymiary") else ""}{znacznik}</div>'
            f'<div style="font-size:13px;color:{SZARY};line-height:1.55;padding-bottom:6px;">{_e(z.get("uzasadnienie"))}</div>'
            f'{_link(z.get("url"), "Zobacz produkt")}</td></tr></table>'
        )
    czesci.append("</td></tr>")
    return "".join(czesci)


def _karta(p: Problem, link_airtable: str) -> str:
    zdjecie = p.zdjecie or p.produkt.zdjecie_url
    miniatura = (
        f'<img src="{_e(zdjecie)}" width="88" height="88" alt="" '
        f'style="display:block;width:88px;height:88px;object-fit:cover;border-radius:4px;background:#ffffff;">'
        if zdjecie
        else ""
    )
    wiersze = [
        _wiersz("W Airtable", _e(p.w_airtable)),
        _wiersz("W sklepie", f"<strong>{_e(p.na_stronie)}</strong>"),
        _wiersz("Co się stało", _e(p.opis), ostatni=not (p.uwaga or p.produkt.wymiar)),
    ]
    if p.produkt.wymiar:
        wiersze.append(_wiersz("Wymiar", _e(p.produkt.wymiar), ostatni=not p.uwaga))
    if p.uwaga:
        wiersze.append(_wiersz("Uwaga", _e(p.uwaga), ostatni=True))
    linki = " &nbsp;&nbsp; ".join(
        x for x in (_link(p.url_koncowy or p.produkt.url, "Otwórz w sklepie"), _link(link_airtable, "Popraw w Airtable")) if x
    )
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:{BEZ};border-radius:6px;margin-bottom:16px;{'border:1.5px solid ' + BURGUND + ';' if p.typ in (ZNIKNAL, NIEDOST) else ''}">
  <tr><td style="padding:22px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        {'<td width="88" style="vertical-align:top;padding-right:16px;">' + miniatura + '</td>' if miniatura else ''}
        <td style="vertical-align:top;">
          {_plakietka(p)}
          <div style="font-size:17px;font-weight:700;color:{TEKST};padding:8px 0 4px;line-height:1.35;">{_e(p.produkt.nazwa)}</div>
        </td>
      </tr>
    </table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;">
      {''.join(wiersze)}
      <tr><td colspan="2" style="padding-top:12px;">{linki}</td></tr>
      {_zamienniki_html(p.zamienniki) if p.typ in (ZNIKNAL, NIEDOST) else ''}
    </table>
  </td></tr>
</table>"""


def _lista_otwartych(problemy: list[Problem], linki: dict[str, str]) -> str:
    if not problemy:
        return ""
    wiersze = []
    for i, p in enumerate(problemy):
        linia = "" if i == len(problemy) - 1 else f"border-bottom:1px solid {LINIA};"
        link = linki.get(p.produkt.id) or p.url_koncowy or p.produkt.url
        wiersze.append(
            f'<tr><td style="padding:9px 0;{linia}font-size:14px;color:{TEKST};">'
            f'<a href="{_e(link)}" style="color:{TEKST};text-decoration:none;">{_e(p.produkt.nazwa)}</a></td>'
            f'<td style="padding:9px 0;{linia}font-size:12px;color:{SZARY};text-align:right;white-space:nowrap;">'
            f'{_e(p.etykieta)}{" · " + _e(p.na_stronie) if p.typ == ZMIANA_CENY else ""}</td></tr>'
        )
    return f"""
<tr><td style="padding-top:28px;">
  <div style="font-size:11px;letter-spacing:0.14em;text-transform:uppercase;color:{BURGUND};font-weight:600;padding-bottom:10px;">
    Wciąż do poprawienia w Airtable ({len(problemy)})</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{BEZ};border-radius:6px;"><tr><td style="padding:12px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(wiersze)}</table>
  </td></tr></table>
</td></tr>"""


def _sortuj(problemy: list[Problem]) -> list[Problem]:
    return sorted(problemy, key=lambda p: (KOLEJNOSC.index(p.typ), p.produkt.nazwa.lower()))


def zbuduj(nowe: list[Problem], pozostale: list[Problem], linki_airtable: dict[str, str],
           podsumowanie: str, przypomnienie: bool = False) -> tuple[str, str]:
    """Zwraca (temat, html)."""
    nowe, pozostale = _sortuj(nowe), _sortuj(pozostale)
    if nowe:
        n = len(nowe)
        temat = f"Interior List: {n} {odmiana(n, 'zmiana', 'zmiany', 'zmian')} w produktach 🔔"
        pilne = sum(1 for p in nowe if p.typ in (ZNIKNAL, NIEDOST))
        if pilne:
            reszta = n - pilne
            temat = (f"Interior List: {pilne} {odmiana(pilne, 'produkt', 'produkty', 'produktów')} do podmiany ⚠️"
                     + (f" (+{reszta} {odmiana(reszta, 'inna zmiana', 'inne zmiany', 'innych zmian')})" if reszta else ""))
        naglowek = f"{n} {odmiana(n, 'nowa zmiana', 'nowe zmiany', 'nowych zmian')} w produktach"
        wstep = ("Sprawdziłem produkty z Twoich list w sklepach. Poniżej to, co według sklepów jest już nieaktualne. "
                 "Niczego nie zmieniałem w Airtable — decyzja należy do Ciebie.")
    else:
        n = len(pozostale)
        temat = f"Interior List: {n} {odmiana(n, 'sprawa czeka', 'sprawy czekają', 'spraw czeka')} na poprawę w Airtable"
        naglowek = "Przypomnienie: listy wciąż mają nieaktualne produkty"
        wstep = ("Dziś nie pojawiło się nic nowego, ale te produkty nadal różnią się od tego, co jest w sklepach. "
                 "Klienci widzą je na listach, które kupili.")

    linki = linki_airtable
    karty = "".join(_karta(p, linki.get(p.produkt.id, "")) for p in nowe)
    if przypomnienie and not nowe:
        karty = "".join(_karta(p, linki.get(p.produkt.id, "")) for p in pozostale)
        lista = ""
    else:
        lista = _lista_otwartych(pozostale, linki)

    html = f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(temat)}</title></head>
<body style="margin:0;padding:0;background:#ffffff;font-family:{FONT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">
  <tr><td style="padding-bottom:20px;border-bottom:1px solid {LINIA};">
    <img src="{config.LOGO_URL}" alt="Interior List" height="36" style="display:block;height:36px;width:auto;">
  </td></tr>
  <tr><td style="padding-top:30px;">
    <span style="font-size:11px;text-transform:uppercase;letter-spacing:0.14em;color:{BURGUND};font-weight:600;">Bot produktów</span>
  </td></tr>
  <tr><td style="padding-top:10px;">
    <h1 style="margin:0;font-size:26px;font-weight:700;color:{TEKST};line-height:1.3;">{_e(naglowek)}</h1>
  </td></tr>
  <tr><td style="padding:14px 0 26px;">
    <p style="margin:0;font-size:15px;line-height:1.7;color:{SZARY};">{_e(wstep)}</p>
  </td></tr>
  <tr><td>{karty}</td></tr>
  {lista}
  <tr><td style="padding-top:32px;border-bottom:1px solid #ddd8d0;"></td></tr>
  <tr><td style="padding-top:16px;font-size:12px;line-height:1.6;color:{ETYKIETA};">
    {_e(podsumowanie)}<br>
    Ceny i dostępność odczytane z danych, które sklepy publikują na stronach produktów. Zamienniki zaproponowało AI —
    każdy link został otwarty i sprawdzony, ale przed podmianą na liście rzuć okiem na produkt.
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
    return temat, html


def wyslij(temat: str, html: str) -> bool:
    if not (config.MAIL_HASLO and config.MAIL_ODBIORCY):
        log.warning("Brak hasła albo odbiorców maila — pomijam wysyłkę.")
        return False
    wiadomosc = MIMEMultipart("alternative")
    wiadomosc["Subject"] = temat
    wiadomosc["From"] = f"Interior List Bot <{config.MAIL_NADAWCA}>"
    wiadomosc["To"] = ", ".join(config.MAIL_ODBIORCY)
    wiadomosc.attach(MIMEText("Raport bota produktów Interior List — otwórz w programie obsługującym HTML.", "plain", "utf-8"))
    wiadomosc.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(config.SMTP_SERWER, config.SMTP_PORT, timeout=60) as serwer:
            serwer.starttls()
            serwer.login(config.MAIL_NADAWCA, config.MAIL_HASLO)
            serwer.sendmail(config.MAIL_NADAWCA, config.MAIL_ODBIORCY, wiadomosc.as_string())
        log.info("📧 Mail wysłany: %s", temat)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.error("Nie udało się wysłać maila: %s", e)
        return False
