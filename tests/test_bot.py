"""Testy na sztucznych stronach sklepów (przypadki z prawdziwych sklepów: JSON-LD, warianty,
mikrodane, meta tagi, miękkie 404, przekierowanie na stronę główną, Allegro, "ostatnie sztuki")."""

import json

import pytest

from bot import config
from bot.airtable_odczyt import Produkt
from bot.ekstrakcja import DOSTEPNY, MALO, NIEDOSTEPNY, analizuj_html, parsuj_cene, status_produktu, wybierz_oferte
from bot.mail import odmiana, zbuduj
from bot.porownanie import BLAD, MALO_SZTUK, NIEDOST, WROCIL, ZMIANA_CENY, ZNIKNAL, ocen
from bot.stan import Stan
from bot.strona import Strona
from bot.zamienniki import dozwolony_sklep, normalizuj_url, parsuj_strumien


def ld(obj) -> str:
    return f'<html><head><script type="application/ld+json">{json.dumps(obj)}</script></head><body>x</body></html>'


def produkt(**kw) -> Produkt:
    dane = dict(id="rec1", nazwa="Stolik nocny Aldo", url="https://jysk.pl/stolik-aldo", cena=299.0,
                wymiar="", status="Dostępny")
    dane.update(kw)
    return Produkt(**dane)


def strona(html="", kod=200, url="https://jysk.pl/stolik-aldo", **kw) -> Strona:
    return Strona(url=url, url_koncowy=kw.pop("url_koncowy", url), kod=kod, html=html, **kw)


def sprawdz(p, html, **kw):
    s = strona(html, **kw)
    return ocen(p, s, analizuj_html(html) if html else None)


# ------------------------------------------------------------ ceny

@pytest.mark.parametrize("tekst,oczekiwana", [
    ("1 599,00 zł", 1599.0), ("1599.00", 1599.0), (1599, 1599.0), ("1.599,99", 1599.99),
    ("1,599.99", 1599.99), ("299", 299.0), ("1.599", 1599.0), ("49,9", 49.9), ("0", None), (None, None),
    ("1\xa0299,00\xa0zł", 1299.0),
])
def test_parsuj_cene(tekst, oczekiwana):
    assert parsuj_cene(tekst) == oczekiwana


# ------------------------------------------------------------ JSON-LD

def test_jsonld_dostepny_bez_zmian():
    html = ld({"@context": "https://schema.org", "@type": "Product", "name": "Aldo",
               "offers": {"@type": "Offer", "price": "299.00", "availability": "https://schema.org/InStock"}})
    assert sprawdz(produkt(), html) == []


def test_jsonld_graph_i_wyprzedany():
    html = ld({"@graph": [{"@type": "WebPage"}, {"@type": ["Product"], "name": "Aldo",
               "offers": [{"@type": "Offer", "price": 299, "availability": "http://schema.org/OutOfStock"}]}]})
    problemy = sprawdz(produkt(), html)
    assert [p.typ for p in problemy] == [NIEDOST]


def test_zmiana_ceny_powyzej_progu():
    html = ld({"@type": "Product", "offers": {"price": "349", "availability": "InStock"}})
    problemy = sprawdz(produkt(), html)
    assert [p.typ for p in problemy] == [ZMIANA_CENY]
    assert problemy[0].nowa_cena == 349.0
    assert "wzrosła" in problemy[0].opis


def test_mala_roznica_ceny_ignorowana():
    html = ld({"@type": "Product", "offers": {"price": "301.99", "availability": "InStock"}})
    assert sprawdz(produkt(), html) == []


def test_warianty_dopasowanie_po_wymiarze():
    html = ld({"@type": "ProductGroup", "name": "Łóżko Nova", "hasVariant": [
        {"@type": "Product", "name": "Łóżko Nova 140x200", "offers": {"price": 1299, "availability": "InStock"}},
        {"@type": "Product", "name": "Łóżko Nova 160x200", "offers": {"price": 1499, "availability": "OutOfStock"}},
    ]})
    p = produkt(cena=1499.0, wymiar="160 x 200 cm")
    problemy = sprawdz(p, html)
    assert [x.typ for x in problemy] == [NIEDOST]  # akurat nasz wariant jest wyprzedany
    p2 = produkt(cena=1299.0, wymiar="140x200")
    assert sprawdz(p2, html) == []


def test_warianty_bez_wymiaru_cena_wystepuje():
    html = ld({"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": "199", "highPrice": "399",
                                               "availability": "InStock"}})
    assert sprawdz(produkt(cena=399.0), html) == []
    problemy = sprawdz(produkt(cena=250.0), html)
    assert [x.typ for x in problemy] == [ZMIANA_CENY]
    assert "199" in problemy[0].na_stronie and "399" in problemy[0].na_stronie


def test_limited_availability_to_malo():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "https://schema.org/LimitedAvailability"}})
    assert [x.typ for x in sprawdz(produkt(), html)] == [MALO_SZTUK]


def test_inventory_level_ponizej_progu():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "InStock",
                                               "inventoryLevel": {"@type": "QuantitativeValue", "value": 4}}})
    assert [x.typ for x in sprawdz(produkt(), html)] == [MALO_SZTUK]


def test_tekst_ostatnie_sztuki_przy_instock():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "InStock"}}).replace(
        "<body>x</body>", "<body><p>Pośpiesz się — ostatnie sztuki!</p></body>")
    assert [x.typ for x in sprawdz(produkt(), html)] == [MALO_SZTUK]


def test_zostalo_n_sztuk():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "InStock"}}).replace(
        "<body>x</body>", "<body><span>Zostało 3 szt.</span></body>")
    assert [x.typ for x in sprawdz(produkt(), html)] == [MALO_SZTUK]
    html2 = html.replace("Zostało 3 szt.", "W magazynie: 150 szt.")
    assert sprawdz(produkt(), html2) == []


def test_wrocil_do_sprzedazy():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "InStock"}})
    assert [x.typ for x in sprawdz(produkt(status="Wycofany"), html)] == [WROCIL]


def test_wycofany_w_airtable_i_w_sklepie_bez_alarmu():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "Discontinued"}})
    assert sprawdz(produkt(status="Wycofany"), html) == []


# ------------------------------------------------------------ mikrodane, meta, tekst

def test_mikrodane():
    html = ('<html><body><div itemscope itemtype="https://schema.org/Product"><span itemprop="price" '
            'content="299.00">299 zł</span><link itemprop="availability" href="https://schema.org/OutOfStock"/>'
            '</div></body></html>')
    assert [x.typ for x in sprawdz(produkt(), html)] == [NIEDOST]


def test_meta_tagi():
    html = ('<html><head><meta property="product:price:amount" content="279.00">'
            '<meta property="product:availability" content="in stock"></head><body></body></html>')
    problemy = sprawdz(produkt(), html)
    assert [x.typ for x in problemy] == [ZMIANA_CENY] and problemy[0].nowa_cena == 279.0


def test_allegro_oferta_zakonczona_bez_danych_strukturalnych():
    html = "<html><body><h1>Stolik</h1><div>Ta oferta jest zakończona</div></body></html>"
    assert [x.typ for x in sprawdz(produkt(url="https://allegro.pl/oferta/123"), html)] == [NIEDOST]


def test_fraza_w_skrypcie_nie_myli():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "InStock"}}).replace(
        "<body>x</body>", "<body><script>var t='Produkt niedostępny';</script>OK</body>")
    assert sprawdz(produkt(), html) == []


# ------------------------------------------------------------ znikanie stron

def test_404():
    assert [x.typ for x in sprawdz(produkt(), "<html>nie ma</html>", kod=404)] == [ZNIKNAL]


def test_404_ale_juz_wycofany_w_airtable():
    assert sprawdz(produkt(status="Wycofany"), "<html></html>", kod=404) == []


def test_miekkie_404():
    html = "<html><head><title>Nie znaleziono strony | JYSK</title></head><body>Ups</body></html>"
    assert [x.typ for x in sprawdz(produkt(), html)] == [ZNIKNAL]


def test_przekierowanie_na_strone_glowna():
    html = "<html><head><title>JYSK</title></head><body>Witamy</body></html>"
    problemy = sprawdz(produkt(), html, url_koncowy="https://jysk.pl/")
    assert [x.typ for x in problemy] == [ZNIKNAL]


def test_kategoria_bez_danych_to_blad_a_nie_wycofanie():
    html = "<html><head><title>Stoliki nocne</title></head><body>Lista produktów</body></html>"
    problemy = sprawdz(produkt(), html, url_koncowy="https://jysk.pl/sypialnia/stoliki")
    assert [x.typ for x in problemy] == [BLAD]


def test_blokada_sklepu():
    html = "<html><title>Just a moment...</title><body>captcha</body></html>"
    problemy = sprawdz(produkt(), html, kod=403, zablokowana=True)
    assert [x.typ for x in problemy] == [BLAD] and "zablokował" in problemy[0].opis


def test_brak_polaczenia():
    s = Strona(url="https://jysk.pl/x", blad="ConnectionError: boom")
    assert [x.typ for x in ocen(produkt(), s, None)] == [BLAD]


# ------------------------------------------------------------ stan

def test_stan_nowe_i_rozwiazane(tmp_path):
    sciezka = str(tmp_path / "stan.json")
    s = Stan()
    akt = {"a": {"typ": "x"}, "b": {"typ": "y"}}
    nowe, rozw = s.porownaj(akt)
    assert nowe == {"a", "b"} and rozw == set()
    s.zatwierdz(akt, nowe, dostarczone=True)
    s.zapisz(sciezka)
    s2 = Stan.wczytaj(sciezka)
    akt2 = {"b": {"typ": "y"}, "c": {"typ": "z"}}
    nowe, rozw = s2.porownaj(akt2)
    assert nowe == {"c"} and rozw == {"a"}
    assert "od" in s2.problemy["b"]


def test_niewyslany_mail_nie_gubi_spraw():
    s = Stan()
    akt = {"a": {"typ": "x"}}
    nowe, _ = s.porownaj(akt)
    s.zatwierdz(akt, nowe, dostarczone=False)
    assert s.porownaj(akt)[0] == {"a"}  # w kolejnym przebiegu nadal "nowe"


def test_cena_wraca_tylko_przy_kolejnej_istotnej_zmianie():
    import sprawdz
    s = Stan()
    k = "rec1:cena"
    s.zatwierdz({k: {"typ": ZMIANA_CENY, "cena": 349.0}}, {k}, dostarczone=True)
    assert s.porownaj({k: {"typ": ZMIANA_CENY, "cena": 350.0}}, sprawdz._cena_sie_zmienila)[0] == set()
    assert s.porownaj({k: {"typ": ZMIANA_CENY, "cena": 399.0}}, sprawdz._cena_sie_zmienila)[0] == {k}


def test_stan_uszkodzony_plik(tmp_path):
    p = tmp_path / "stan.json"
    p.write_text("{zepsute")
    assert Stan.wczytaj(str(p)).problemy == {}


def test_klucz_ceny_staly():
    a = sprawdz(produkt(), ld({"@type": "Product", "offers": {"price": 349, "availability": "InStock"}}))[0]
    b = sprawdz(produkt(), ld({"@type": "Product", "offers": {"price": 399, "availability": "InStock"}}))[0]
    assert a.klucz == b.klucz == "rec1:cena"


# ------------------------------------------------------------ zamienniki

@pytest.mark.parametrize("url,ok", [
    ("https://www.jysk.pl/sypialnia/stolik-123", True),
    ("https://jysk.pl/x", True),
    ("https://www.ikea.com/pl/pl/p/hemnes-123/", True),
    ("https://www.ikea.com/de/de/p/hemnes-123/", False),
    ("https://www.sklum.com/pl/kupic-stoliki/123", True),
    ("https://www.sklum.com/es/x", False),
    ("https://allegro.pl/oferta/stolik-123", True),
    ("https://fake-jysk.pl/x", False),
    ("https://jysk.pl.evil.com/x", False),
    ("https://amazon.pl/x", False),
    ("ftp://jysk.pl/x", False),
    ("", False),
])
def test_dozwolone_sklepy(url, ok):
    assert bool(dozwolony_sklep(url)) is ok


def test_normalizacja_url():
    assert normalizuj_url("https://www.jysk.pl/a/?utm_source=x&id=5#top") == normalizuj_url("https://jysk.pl/a?id=5")


def test_parsuj_strumien():
    linie = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "WebFetch", "input": {"url": "https://jysk.pl/a?utm_x=1"}},
            {"type": "tool_use", "id": "t2", "name": "WebFetch", "input": {"url": "https://brw.pl/b"}},
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "403", "is_error": True},
        ]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
                    "structured_output": {"propozycje": [], "uwagi": "nic"}}),
    ]
    wynik, otwarte, meta = parsuj_strumien(linie)
    assert wynik == {"propozycje": [], "uwagi": "nic"}
    assert otwarte == {normalizuj_url("https://jysk.pl/a")}
    assert meta["num_turns"] == 7


def test_prompt_sie_formatuje():
    from bot.zamienniki import zbuduj_prompt
    tekst = zbuduj_prompt(produkt(wymiar="45x40x55"), "produkt.jpg", "")
    assert "Stolik nocny Aldo" in tekst and "produkt.jpg" in tekst and "site:" in tekst
    assert "{" not in tekst.replace("{nazwa}", "")  # brak niewypełnionych pól


# ------------------------------------------------------------ mail

def test_odmiana():
    assert [odmiana(n, "zmiana", "zmiany", "zmian") for n in (1, 2, 5, 12, 22, 25)] == \
           ["zmiana", "zmiany", "zmian", "zmian", "zmiany", "zmian"]


def test_mail_escapuje_i_ma_temat():
    p = produkt(nazwa='Stolik <script>alert(1)</script> "Aldo"')
    problemy = sprawdz(p, "<html></html>", kod=404)
    problemy[0].zamienniki = {"propozycje": [{"nazwa": "Hemnes", "sklep": "IKEA", "url": "https://ikea.com/pl/x",
                                              "cena": 349.0, "cena_ze_strony": True, "wymiary": "46x35",
                                              "uzasadnienie": "Biel i proste fronty.", "zdjecie": "",
                                              "weryfikacja": "sprawdzony"}], "uwagi": ""}
    temat, html = zbuduj(problemy, [], {"rec1": "https://airtable.com/app/tbl/rec1"}, "Sprawdzono 1 produkt")
    assert "do podmiany" in temat
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Hemnes" in html and "Popraw w Airtable" in html


def test_status_produktu_cena_bez_dostepnosci_to_dostepny():
    w = analizuj_html(ld({"@type": "Product", "offers": {"price": 299}}))
    assert status_produktu(w, wybierz_oferte(w, ""))[0] == DOSTEPNY


# ------------------------------------------------------------ przypadki z przeglądu kodu (fałszywe "wycofany")

def test_ukryta_fraza_nie_wycofuje():
    html = ld({"@type": "Product", "offers": {"price": 299}}).replace(
        "<body>x</body>", '<body><span style="display:none">Produkt chwilowo niedostępny</span>Kup teraz</body>')
    assert sprawdz(produkt(), html) == []


def test_slaba_fraza_przy_cenie_nie_wycofuje():
    html = ld({"@type": "Product", "offers": {"price": 299}}).replace(
        "<body>x</body>", "<body><div>Inny stolik — chwilowo niedostępny</div></body>")
    assert sprawdz(produkt(), html) == []


def test_mocna_fraza_przy_cenie_wycofuje():
    html = ld({"@type": "Product", "offers": {"price": 299}}).replace(
        "<body>x</body>", "<body><h2>Ta oferta jest zakończona</h2></body>")
    assert [x.typ for x in sprawdz(produkt(), html)] == [NIEDOST]


def test_stany_w_salonach_nie_wycofuja():
    html = ld({"@type": "Product", "offers": {"price": 299}}).replace(
        "<body>x</body>", "<body>Warszawa: zostało 0 szt. Kraków: zostało 12 szt.</body>")
    assert sprawdz(produkt(), html) == []


def test_przerwa_techniczna_503_to_blad():
    html = "<html><body>Sklep chwilowo niedostępny — przerwa techniczna</body></html>"
    assert [x.typ for x in sprawdz(produkt(), html, kod=503)] == [BLAD]


def test_wymiar_bez_dokladnego_wariantu_ocenia_wszystkie():
    html = ld({"@type": "ProductGroup", "hasVariant": [
        {"@type": "Product", "name": "Łóżko 140x200", "offers": {"price": 1299, "availability": "OutOfStock"}},
        {"@type": "Product", "name": "Łóżko 160x200", "offers": {"price": 1499, "availability": "InStock"}},
    ]})
    assert sprawdz(produkt(cena=1499.0, wymiar="90x200"), html) == []


def test_backorder_z_zerem_sztuk_nie_wycofuje():
    html = ld({"@type": "Product", "offers": {"price": 299, "availability": "BackOrder", "inventoryLevel": 0}})
    assert [x.typ for x in sprawdz(produkt(), html)] == [MALO_SZTUK]


def test_nieskonczonosc_w_json_ld_nie_wywraca():
    html = '<script type="application/ld+json">{"@type":"Product","offers":{"price":299,"inventoryLevel":Infinity}}</script>'
    sprawdz(produkt(), html)  # nie rzuca wyjątku


def test_zakres_aggregateoffer_nie_jest_wariantem():
    html = ld({"@type": "Product", "name": "Dywan 160x230", "offers": {"@type": "AggregateOffer",
               "lowPrice": 299, "highPrice": 899, "availability": "InStock"}})
    assert sprawdz(produkt(cena=899.0, wymiar="160x230"), html) == []


@pytest.mark.parametrize("tekst,oczekiwana", [
    ("1\u2009599,00 zł", 1599.0), ("1599.000", 1599.0), ("1 599,00 zł (23% VAT)", 1599.0),
    ("1 299 – 1 599 zł", 1299.0), ("1\t599,00", 1599.0),
])
def test_parsuj_cene_trudne(tekst, oczekiwana):
    assert parsuj_cene(tekst) == oczekiwana


def test_produkt_w_parametrach_to_nie_strona_glowna():
    s = Strona(url="https://sklep.pl/?product_id=5", url_koncowy="https://sklep.pl/?product_id=5", kod=200)
    assert not s.na_strone_glowna
    s2 = Strona(url="https://sklep.pl/stolik-5", url_koncowy="https://sklep.pl/", kod=200)
    assert s2.na_strone_glowna


def test_ikea_plus_to_nie_pl():
    assert dozwolony_sklep("https://www.ikea.com/plus/x") is None
    assert dozwolony_sklep("https://www.ikea.com/pl") == "IKEA"


def test_claude_nie_dostaje_hasel(monkeypatch):
    from bot.zamienniki import _srodowisko_dla_claude
    monkeypatch.setenv("AIRTABLE_TOKEN", "x")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ok")
    env = _srodowisko_dla_claude()
    assert "AIRTABLE_TOKEN" not in env and "GMAIL_APP_PASSWORD" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ok" and "PATH" in env


# ------------------------------------------------------------ łóżka zawsze 160x200

@pytest.mark.parametrize("nazwa,wynik", [
    ("Łóżko Theo", "160x200"), ("Rama łóżka Nordic", "160x200"), ("Łóżko tapicerowane Velvet", "160x200"),
    ("Tapicerowane łóżko Ava", "160x200"), ("Narzuta na łóżko Boho", ""), ("Pościel do łóżka len", ""),
    ("Stolik nocny przy łóżku", ""), ("Sofa Marsala", ""),
])
def test_staly_wariant(nazwa, wynik):
    from bot.porownanie import staly_wariant
    assert staly_wariant(produkt(nazwa=nazwa)) == wynik


def test_lozko_cena_mniejszego_rozmiaru_nie_alarmuje():
    # Konfigurator: w danych strony tylko cena najmniejszego rozmiaru.
    html = ld({"@type": "Product", "name": "Łóżko Theo", "offers": {"price": 3699, "availability": "InStock"}})
    assert sprawdz(produkt(nazwa="Łóżko Theo", cena=3958.0), html) == []


def test_lozko_wariant_160_z_json_ld():
    html = ld({"@type": "ProductGroup", "name": "Łóżko Theo", "hasVariant": [
        {"@type": "Product", "name": "Łóżko Theo 140x200", "offers": {"price": 3699, "availability": "InStock"}},
        {"@type": "Product", "name": "Łóżko Theo 160x200", "offers": {"price": 4199, "availability": "InStock"}},
    ]})
    problemy = sprawdz(produkt(nazwa="Łóżko Theo", cena=3958.0), html)
    assert [x.typ for x in problemy] == [ZMIANA_CENY] and problemy[0].nowa_cena == 4199.0


def test_lozko_wariant_160_z_woocommerce():
    warianty = json.dumps([
        {"attributes": {"attribute_pa_rozmiar": "140x200"}, "display_price": 3699, "variation_id": 1},
        {"attributes": {"attribute_pa_rozmiar": "160x200"}, "display_price": 3958, "variation_id": 2},
    ]).replace('"', "&quot;")
    html = ld({"@type": "Product", "name": "Łóżko Theo", "offers": {"price": 3699, "availability": "InStock"}}).replace(
        "<body>x</body>", f'<body><form class="variations_form" data-product_variations="{warianty}"></form></body>')
    assert sprawdz(produkt(nazwa="Łóżko Theo", cena=3958.0), html) == []
    assert [x.typ for x in sprawdz(produkt(nazwa="Łóżko Theo", cena=3500.0), html)] == [ZMIANA_CENY]


def test_lozko_shopify_ceny_w_groszach():
    dane = json.dumps({"product": {"title": "Łóżko Theo", "variants": [
        {"title": "140 x 200", "price": 369900, "compare_at_price": None},
        {"title": "160 x 200", "price": 395800, "compare_at_price": None},
    ]}})
    html = ld({"@type": "Product", "offers": {"price": 3699, "availability": "InStock"}}).replace(
        "<body>x</body>", f'<body><script type="application/json" data-product-json>{dane}</script></body>')
    assert sprawdz(produkt(nazwa="Łóżko Theo", cena=3958.0), html) == []


def test_lozko_niedostepne_nadal_wykrywane():
    html = ld({"@type": "Product", "name": "Łóżko Theo", "offers": {"price": 3699, "availability": "OutOfStock"}})
    assert [x.typ for x in sprawdz(produkt(nazwa="Łóżko Theo", cena=3958.0), html)] == [NIEDOST]


def test_polecany_materac_160_nie_myli_ceny():
    dane = json.dumps({"recommended": [{"name": "Materac 160x200", "price": 1299}]})
    html = ld({"@type": "Product", "offers": {"price": 3699, "availability": "InStock"}}).replace(
        "<body>x</body>", f'<body><script type="application/json">{dane}</script></body>')
    assert sprawdz(produkt(nazwa="Łóżko Theo", cena=3958.0), html) == []
