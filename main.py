import os
import smtplib
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
import json

from google.genai import types
from pyairtable import Api
import time

from prompty.prompt_analiza_informacji_produkt import pobierz_prompt_analizy_informacji_produktu
from prompty.prompt_zamienniki import pobierz_prompt_zamiennikow

load_dotenv()


def generuj_zamienniki_od_ai(client, nazwa, cena, poszukiwany_wymiar, product_image_url, room_image_url, old_product_url):
    """Funkcja pobiera zdjęcia, wysyła je do Gemini i zwraca gotowy kod HTML z zamiennikami."""

    zamienniki_html = "<p><i>Brak zdjęcia w bazie, AI nie mogło wygenerować zamienników.</i></p>"

    if not product_image_url:
        return zamienniki_html  # Jeśli nie ma nawet zdjęcia głównego, od razu zwracamy komunikat

    print("⏳ Odczekuję 15s przed zapytaniem o zamienniki (ochrona API)...")
    time.sleep(15)
    print("📸 Wysyłam dane do Gemini z prośbą o zamienniki (Włączony dostęp do Internetu)...")

    for proba_zam in range(3):
        try:
            # 1. Pobieranie zdjęcia głównego (obowiązkowe)
            img_product_data = requests.get(product_image_url).content

            # 2. Składanie paczki dla AI
            prompt = pobierz_prompt_zamiennikow(nazwa, cena, poszukiwany_wymiar, old_product_url)

            zawartosc_dla_ai = [
                prompt,
                types.Part.from_bytes(data=img_product_data, mime_type='image/jpeg')
            ]

            # 3. Zabezpieczenie: Pobieramy zdjęcie pokoju TYLKO, jeśli link istnieje
            if room_image_url:
                img_room_data = requests.get(room_image_url).content
                zawartosc_dla_ai.append(types.Part.from_bytes(data=img_room_data, mime_type='image/jpeg'))

            # 4. Strzał do AI
            odp_zamienniki = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=zawartosc_dla_ai,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )

            # 5. Wyciągnięcie tekstu
            zamienniki_html = odp_zamienniki.candidates[0].content.parts[0].text.strip().replace("```html", "").replace(
                "```", "")
            break  # Sukces, wychodzimy z pętli prób

        except Exception as e_zam:
            if '429' in str(e_zam) or 'RESOURCE_EXHAUSTED' in str(e_zam):
                print(f"⚠️ Limit API (zamienniki). Czekam 35s... (Próba {proba_zam + 1}/3)")
                time.sleep(35)
            else:
                print(f"❌ Błąd AI przy zamiennikach: {e_zam}")
                zamienniki_html = f"<p><i>Wystąpił błąd AI: {e_zam}</i></p>"
                break

    return zamienniki_html

# --- 1.1 KONFIGURACJA KLUCZY ---
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
BASE_ID = os.getenv("BASE_ID")
TABLE_NAME = "Products"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- 1.2 KONFIGURACJA MAILA (GMail) ---
MAIL_CONFIG = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,

    "sender_email": "interiorlist.bot@gmail.com",
    # <--- TUTAJ wpisz adres mailowy tego nowego bota

    "app_password": os.getenv("GMAIL_APPPASWORD"),
    # <--- O TUTAJ WKLEJASZ HASŁO APLIKACJI (usuń spacje, zostaw cudzysłowy!)

    "receiver_email": os.getenv("GMAIL_RECEIVER")
    # <--- TUTAJ wpisz swój prywatny/firmowy mail, na który chcesz dostawać powiadomienia
}

# --- 2. INICJALIZACJA POŁĄCZEŃ ---
print("🔌 Inicjalizacja połączeń z Airtable i Gemini...")
api = Api(AIRTABLE_TOKEN)
table = api.table(BASE_ID, TABLE_NAME)
client = genai.Client(api_key=GEMINI_API_KEY)

unavailable_products = []

# --- 3. GŁÓWNA PĘTLA ---
try:
    print("📥 Pobieram produkty z bazy...\n")
    records = table.all(
        formula="AND( {Product Name} = 'Stolik nocny Aldo SN 54',{Affiliate Link} != '')", # formuła testowa
        # formula="NOT({Affiliate Link} = '')", # formuła produkcyjna
        max_records=2 # zawezenie testowe
    )

    if not records:
        print("Brak produktów z wypełnionym linkiem do sprawdzenia.")

    for record in records:
        record_id = record['id']
        fields = record['fields']

        nazwa = fields.get('Product Name', 'Nieznany produkt')
        url = fields.get('Affiliate Link')
        cena = fields.get('Price')
        poszukiwany_wymiar = fields.get('Dimension', '')

        zdjecia_produktu_airtable = fields.get('Product Image', [])
        image_url = ""
        if zdjecia_produktu_airtable and len(zdjecia_produktu_airtable) > 0:
            image_url = zdjecia_produktu_airtable[0].get('url', '')

        zdjecia_pokoju_airtable = fields.get('Cover Render Image', [])
        room_image_url = ""
        if zdjecia_pokoju_airtable and len(zdjecia_pokoju_airtable) > 0:
            room_image_url = zdjecia_pokoju_airtable[0].get('url', '')

        print(f"📦 Biorę pod lupę: {nazwa}")
        print(f"🔗 Wchodzę na: {url}")

        czysty_tekst = ""
        status_z_404 = None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 404:
                print("❌ Błąd 404: Strona nie istnieje.")
                print("💾 Oznaczam w Airtable jako 'Wycofany' i dodaję do maila.")
                table.update(record_id, {'Availability Status': 'Wycofany'})
                status_z_404 = "Wycofany"

                zamienniki_html = generuj_zamienniki_od_ai(client, nazwa, cena, poszukiwany_wymiar, image_url, room_image_url, url)

                unavailable_products.append({
                    "id": record_id, "name": nazwa, "url": url, "dimension": poszukiwany_wymiar, "price": cena,
                    "image_url": image_url, "ai_suggestions": zamienniki_html
                })

                print("-" * 30)
                print("⏳ Czekam 20 sekund przed kolejnym produktem...")
                time.sleep(20)
                continue

            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            czysty_tekst = soup.get_text(separator=' ', strip=True)[:15000]
            print("✅ Strona pobrana pomyślnie.")

        except Exception as e_req:
            print(f"❌ Nie udało się załadować strony sklepu: {e_req}")
            print("-" * 30)
            time.sleep(15)
            continue

        if czysty_tekst and not status_z_404:
            print("🧠 AI analizuje stronę i szuka ceny...")
            prompt = pobierz_prompt_analizy_informacji_produktu(czysty_tekst, poszukiwany_wymiar)

            ai_response = None
            for proba in range(3):
                try:
                    ai_response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=prompt
                    )
                    break
                except Exception as e_ai:
                    if '429' in str(e_ai) or 'RESOURCE_EXHAUSTED' in str(e_ai):
                        print(f"⚠️ Limit minutowy osiągnięty. Zatrzymuję bota na 35 sekund... (Próba {proba + 1}/3)")
                        time.sleep(35)
                    else:
                        print(f"❌ Inny błąd AI podczas analizy strony: {e_ai}")
                        break

            if ai_response:
                try:
                    odpowiedz_tekst = ai_response.text.strip()
                    if odpowiedz_tekst.startswith('```json'):
                        odpowiedz_tekst = odpowiedz_tekst[7:-3].strip()
                    elif odpowiedz_tekst.startswith('```'):
                        odpowiedz_tekst = odpowiedz_tekst[3:-3].strip()

                    dane_z_ai = json.loads(odpowiedz_tekst)
                    cena = dane_z_ai.get('cena')
                    status = dane_z_ai.get('status')

                    print(f" Wynik AI -> Cena: {cena} PLN | Status: {status}")

                    dane_do_aktualizacji = {}
                    if cena is not None: dane_do_aktualizacji['Price'] = cena
                    if status is not None: dane_do_aktualizacji['Availability Status'] = status

                    if status == "Wycofany":
                        print("➕ Produkt niedostępny, generuję zamienniki i dodaję do maila.")
                        zamienniki_html = generuj_zamienniki_od_ai(client, nazwa, cena, poszukiwany_wymiar, image_url, room_image_url, url)

                        unavailable_products.append({
                            "id": record_id, "name": nazwa, "url": url, "dimension": poszukiwany_wymiar, "price": cena,
                            "image_url": image_url, "ai_suggestions": zamienniki_html
                        })

                    if dane_do_aktualizacji:
                        table.update(record_id, dane_do_aktualizacji)
                        print(f"💾 Sukces! Zapisano w Airtable.")

                except Exception as e_parsing:
                    print(f"❌ Błąd przetwarzania: {e_parsing}")

        print("-" * 30)
        print("⏳ Czekam 20 sekund przed kolejnym produktem (limit API)...")
        time.sleep(20)

except Exception as e:
    print(f"Krytyczny błąd: {e}")

# --- 7. WYSYŁANIE MAILA ---
if unavailable_products:
    print(f"\n📧 Przygotowuję maila z listą {len(unavailable_products)} niedostępnych produktów...")
    html_body = f"""
    <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <h2 style="color: #d9534f;">🚨 Alert: Wykryto niedostępne produkty!</h2>
            <ul style="list-style-type: none; padding: 0;">
    """
    for prod in unavailable_products:
        html_body += f"""
                    <li style="margin-bottom: 40px; border: 1px solid #ddd; padding: 15px; border-radius: 8px;">
                        <h3 style="margin-top: 0; color: #333;">{prod['name']}</h3>
                        <p>
                            <b>Wymiar wg Airtable:</b> {prod['dimension']}<br>
                            <b>Ostatnia cena:</b> {prod.get('price', 'Brak danych')} PLN<br>
                            <a href="{prod['url']}" style="color: #0066cc;">🔗 Stary link</a>
                        </p>
                """
        if prod.get('image_url'):
            html_body += f'<img src="{prod["image_url"]}" style="max-width: 200px; border-radius: 5px;"><br>'
        html_body += f"""
                <div style="background-color: #f9f9f9; padding: 15px; margin-top: 15px; border-radius: 5px;">
                    <h4 style="margin-top: 0;">🤖 Zamienniki zaproponowane przez AI:</h4>
                    {prod['ai_suggestions']}
                </div>
            </li>
        """
    html_body += "</ul></body></html>"

    msg = MIMEText(html_body, 'html', 'utf-8')
    msg['Subject'] = f"🚨 Alert: {len(unavailable_products)} produktów niedostępnych (z zamiennikami AI)"
    msg['From'] = MAIL_CONFIG['sender_email']
    msg['To'] = MAIL_CONFIG['receiver_email']

    try:
        with smtplib.SMTP(MAIL_CONFIG['smtp_server'], MAIL_CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(MAIL_CONFIG['sender_email'], MAIL_CONFIG['app_password'])
            server.send_message(msg)
        print("✅ Mail z inteligentnym alertem wysłany pomyślnie.")
    except Exception as e_mail:
        print(f"❌ Nie udało się wysłać maila: {e_mail}")
else:
    print("\n✅ Wszystkie sprawdzane produkty są dostępne.")