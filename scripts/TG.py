import os
import time
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

def tg_login_and_enter(page):
    login_url = os.getenv("TRAINING_LOGIN_URL", "https://app.tgmanager.com/auth")
    user = os.getenv("TRAINING_USER")
    pwd = os.getenv("TRAINING_PASS")

    print("➡ TG: abriendo login...")
    page.goto(login_url, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")

    # Inputs del login (por las capturas: 1 texto + 1 password)
    page.locator("input[type='text'], input:not([type])").first.fill(user)
    page.locator("input[type='password']").first.fill(pwd)

    # Botón ACCEDER
    page.get_by_role("button", name="ACCEDER").click()
    page.wait_for_load_state("networkidle", timeout=90_000)

    print("➡ TG: esperando selector de centro...")
    # Aquí aparece el combo "Seleccionar centro"
    # Esperamos el input/combobox visible
    page.get_by_text("Seleccionar centro", exact=False).wait_for(timeout=30_000)

    center_name = os.getenv("TRAINING_CENTER_NAME", "UltraGym & Fitness - Azahares")

    print(f"➡ TG: seleccionando centro: {center_name}")
    # Abre dropdown (clic sobre el campo)
    page.get_by_text("Seleccionar centro", exact=False).click()

    # Click en la opción del centro (texto exacto de la lista)
    page.get_by_text(center_name, exact=True).click()

    # Clic en ACCEDER (ahora sí habilitado)
    page.get_by_role("button", name="ACCEDER").click()
    page.wait_for_load_state("networkidle", timeout=90_000)

    print("✔ TG: login + centro OK")


def tg_goto_workout_report(page):
    url = os.getenv("TRAINING_REPORT_WORKOUT_URL", "https://app.tgmanager.com/reports/workout")
    print("➡ TG: yendo a reporte Rutinas y pesajes...")
    page.goto(url, timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=90_000)

    # Señal simple de que cargó algo del reporte
    # (en tu captura se ve "INICIO > PANEL DE CONTROL > USOS" y pestañas)
    # Si esto no existe, lo ajustamos a un texto real del reporte.
    page.get_by_text("PANEL DE CONTROL", exact=False).wait_for(timeout=30_000)

    print("✔ TG: ya estás en /reports/workout")
