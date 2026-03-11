# main.py
import os
import re
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from scraper import (
    init_client,
    scrape_profile_and_connections,
    scrape_profile_by_url,
)

load_dotenv()
MAX_CONTACTS = int(os.getenv("MAX_CONTACTS", 20))

def extract_username(url: str) -> str:
    match = re.search(r'linkedin\.com/in/([^/?]+)', url)
    if not match:
        raise ValueError(f"URL inválida: {url}")
    return match.group(1).rstrip('/')

def main():
    print("¿Qué quieres scrapear?")
    print("1) Conexiones de mi cuenta (perfil + contactos con emails/teléfonos)")
    print("2) Un perfil por URL (mismos datos que en conexiones; sin lista de sus contactos)")
    mode = input("👉 Elige 1 o 2: ").strip()

    url = input("\n🔗 Pega la URL del perfil de LinkedIn: ").strip()
    username = extract_username(url)

    account = init_client()

    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')

    if mode == "2":
        # Perfil por URL: perfil + conexiones en común / personas que quizá conozcas (si el navegador las muestra)
        perfil, sugeridos = scrape_profile_by_url(account, url)
        df_perfil = pd.DataFrame([perfil])
        f_perfil = f"output/perfil_url_{username}_{timestamp}.csv"
        df_perfil.to_csv(f_perfil, index=False, encoding="utf-8-sig")
        print(f"\n✅ Perfil guardado en: {f_perfil}")
        if sugeridos:
            df_sug = pd.DataFrame(sugeridos)
            f_sug = f"output/sugeridos_url_{username}_{timestamp}.csv"
            df_sug.to_csv(f_sug, index=False, encoding="utf-8-sig")
            print(f"✅ {len(sugeridos)} sugerencias (conexiones en común / PYMK) guardadas en: {f_sug}")
        print("ℹ️  La lista completa de contactos de ese perfil no es accesible; solo lo que LinkedIn muestra (conexiones en común, personas que quizá conozcas).")
        return

    # Modo por defecto: conexiones de mi cuenta
    perfil, conexiones = scrape_profile_and_connections(account, username, MAX_CONTACTS)

    # Guardar perfil principal (puede estar parcialmente vacío si StaffSpy falla)
    df_perfil = pd.DataFrame([perfil])
    f_perfil = f"output/perfil_{username}_{timestamp}.csv"
    df_perfil.to_csv(f_perfil, index=False, encoding="utf-8-sig")
    print(f"\n✅ Perfil guardado en: {f_perfil}")

    # Guardar conexiones
    if not conexiones.empty:
        f_conexiones = f"output/conexiones_{username}_{timestamp}.csv"
        conexiones.to_csv(f_conexiones, index=False, encoding="utf-8-sig")
        print(f"✅ {len(conexiones)} conexiones guardadas en: {f_conexiones}")

        cols = [c for c in ["name", "position", "company", "location", "emails"] if c in conexiones.columns]
        print(conexiones[cols].head(10))
    else:
        print("ℹ️  No se obtuvieron conexiones")

if __name__ == "__main__":
    main()