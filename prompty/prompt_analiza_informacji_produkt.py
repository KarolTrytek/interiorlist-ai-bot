def pobierz_prompt_analizy_informacji_produktu(czysty_tekst, poszukiwany_wymiar):
    return f"""
    Przeanalizuj poniższy tekst pobrany ze strony sklepu internetowego z meblami:
    ---
    {czysty_tekst}
    ---
    INFORMACJA O POSZUKIWANYM WARIACIE (WYMIARZE):
    Poszukiwany wymiar to: "{poszukiwany_wymiar}".
    (Jeśli to pole jest puste, po prostu znajdź główną cenę produktu).

    Instrukcje dotyczące CENY:
    - Znajdź cenę dla podanego wariantu (akceptuj drobne różnice np. 180 vs 183 cm).
    - Zwróć samą liczbę (typ float) z kropką dziesiętną, np. 1599.00.

    Instrukcje dotyczące STATUSU - BARDZO WAŻNE (użyj DOKŁADNIE jednego z tych 3 słów):
    1. "Dostępny" -> UŻYJ ZAWSZE JAKO DOMYŚLNY STATUS, jeśli widzisz cenę, a w tekście NIE MA wyraźnej informacji o braku towaru. Zakładamy, że produkt można kupić.
    2. "Wycofany" -> UŻYJ TYLKO I WYŁĄCZNIE, gdy na stronie jest WPROST napisane, że produktu nie da się kupić. ABSOLUTNIE NIE ZGADUJ.
    3. "Mało sztuk" -> UŻYJ TYLKO, gdy na stronie jest wprost napisane np. "ostatnie sztuki", LUB liczba dostępnych sztuk jest MNIEJSZA NIŻ 20.

    Format odpowiedzi (KRYTYCZNE):
    Odpowiedz TYLKO I WYŁĄCZNIE w formacie JSON. Bez formatowania Markdown.
    Wzorzec: {{"cena": 1599.00, "status": "Dostępny"}}
    """