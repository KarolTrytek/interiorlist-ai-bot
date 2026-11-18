"""Pamięć bota między przebiegami (plik stan/stan.json w repozytorium).

Dzięki niej mail przychodzi tylko wtedy, gdy pojawi się coś NOWEGO, a zamienniki
dla danego produktu są szukane tylko raz."""

from __future__ import annotations

import json
import os
from datetime import datetime

from . import config


def teraz() -> datetime:
    return datetime.now(config.STREFA)


class Stan:
    def __init__(self, dane: dict | None = None):
        dane = dane or {}
        # klucz problemu -> {"od": iso, "typ": ..., "produkt": nazwa}
        self.problemy: dict[str, dict] = dane.get("problemy", {})
        # id rekordu -> liczba kolejnych nieudanych sprawdzeń
        self.bledy: dict[str, int] = dane.get("bledy", {})
        # id rekordu -> {"data": iso, "propozycje": [...], "uwagi": str}
        self.zamienniki: dict[str, dict] = dane.get("zamienniki", {})
        # id rekordu -> ile przebiegów z rzędu produkt wygląda na wycofany (czeka na potwierdzenie)
        self.podejrzane: dict[str, int] = dane.get("podejrzane", {})
        # data (YYYY-MM-DD) ostatniego wysłanego maila
        self.ostatni_mail: str = dane.get("ostatni_mail", "")

    @classmethod
    def wczytaj(cls, sciezka: str = config.SCIEZKA_STANU) -> "Stan":
        try:
            with open(sciezka, encoding="utf-8") as f:
                return cls(json.load(f))
        except FileNotFoundError:
            return cls()
        except json.JSONDecodeError:
            # Uszkodzony plik nie może zablokować bota — zaczynamy od zera.
            return cls()

    def zapisz(self, sciezka: str = config.SCIEZKA_STANU) -> None:
        os.makedirs(os.path.dirname(sciezka) or ".", exist_ok=True)
        dane = {
            "wersja": 1,
            "problemy": dict(sorted(self.problemy.items())),
            "bledy": dict(sorted(self.bledy.items())),
            "podejrzane": dict(sorted(self.podejrzane.items())),
            "zamienniki": dict(sorted(self.zamienniki.items())),
            "ostatni_mail": self.ostatni_mail,
        }
        tymczasowy = sciezka + ".tmp"
        with open(tymczasowy, "w", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tymczasowy, sciezka)

    def porownaj(self, aktualne: dict[str, dict], zmienione=None) -> tuple[set[str], set[str]]:
        """Nic nie zapisuje. Zwraca (nowe, rozwiązane). `zmienione(stare, nowe) -> bool` pozwala
        uznać znaną sprawę za nową (np. cena znowu się zmieniła)."""
        nowe = {
            k for k, v in aktualne.items()
            if k not in self.problemy or (zmienione is not None and zmienione(self.problemy[k], v))
        }
        return nowe, set(self.problemy) - set(aktualne)

    def zatwierdz(self, aktualne: dict[str, dict], nowe: set[str], dostarczone: bool) -> None:
        """Zapisuje listę otwartych spraw. Nowe sprawy trafiają do pamięci tylko wtedy, gdy mail
        o nich wyszedł — inaczej w kolejnym przebiegu znowu będą „nowe” i nie przepadną."""
        stamp = teraz().isoformat(timespec="minutes")
        wynik = {}
        for k, v in aktualne.items():
            if k in nowe:
                if dostarczone:
                    wynik[k] = {**v, "od": stamp}
                elif k in self.problemy:
                    wynik[k] = self.problemy[k]
            else:
                wynik[k] = self.problemy.get(k) or {**v, "od": stamp}
        self.problemy = wynik

    def wyczysc_zamienniki(self, nadal_wycofane: set[str], dni: int = 7) -> None:
        """Zapomina zamienniki produktów, które od tygodnia nie są wycofane (np. poprawione w Airtable).
        Tydzień zapasu chroni przed ponownym szukaniem, gdy sklep na chwilę "mignie" produktem."""
        granica = teraz().timestamp() - dni * 86400

        def swiezy(v: dict) -> bool:
            try:
                return datetime.fromisoformat(v.get("data", "")).timestamp() > granica
            except (ValueError, TypeError):
                return False

        self.zamienniki = {k: v for k, v in self.zamienniki.items() if k in nadal_wycofane or swiezy(v)}

    def mail_dzisiaj(self) -> bool:
        return self.ostatni_mail == teraz().date().isoformat()

    def oznacz_mail(self) -> None:
        self.ostatni_mail = teraz().date().isoformat()
