import os
import sys
import time
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

import pytz
import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TZ = pytz.timezone("America/Tijuana")

# ================= Configuración general ================= #
BASE_DIR = Path(__file__).resolve().parents[1]  # TRACK BI
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "socios_con_y_sin_rutina.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

# ---------- GASCA ----------
USER      = os.getenv("DIRECCION_USER")
PASS      = os.getenv("DIRECCION_PASS")
LOGIN_URL = os.getenv("DIRECCION_LOGIN_URL")
KPI_URL   = os.getenv("KPI_DESEMPENO_URL", "https://ultragimnasios.com/Modulo/Kpis/Index")

# ---------- TRAINING GYM ----------
TRAINING_LOGIN_URL = os.getenv("TRAINING_LOGIN_URL", "https://app.tgmanager.com/auth")
TRAINING_REPORT_WORKOUT_URL = os.getenv("TRAINING_REPORT_WORKOUT_URL", "https://app.tgmanager.com/reports/workout")

# Credenciales TG en .env (NO hardcode)
TRAINING_USER = os.getenv("TRAINING_USER")
TRAINING_PASS = os.getenv("TRAINING_PASS")
TRAINING_CENTER_NAME = os.getenv("TRAINING_CENTER_NAME", "UltraGym & Fitness - Azahares")

# Estado persistente TG (cookies/localStorage)
TG_STATE_PATH = Path(os.getenv("TRAINING_STATE_PATH", str(BASE_DIR / "tg_state.json"))).resolve()

SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"
MAX_RETRIES = 3
DOWNLOAD_TIMEOUT_MS = 120_000

OUT_DIR = (BASE_DIR / "data" / "rutinas").resolve()
RAW_DIR = (OUT_DIR / "raw").resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)


def validar_config():
    faltan = []
    if not USER:      faltan.append("DIRECCION_USER")
    if not PASS:      faltan.append("DIRECCION_PASS")
    if not LOGIN_URL: faltan.append("DIRECCION_LOGIN_URL")

    # TG: puedes dejarlo sin credenciales si usarás modo manual/captura
    # pero ideal que existan para intentar login automático más adelante
    if not TRAINING_USER: faltan.append("TRAINING_USER (TG)")
    if not TRAINING_PASS: faltan.append("TRAINING_PASS (TG)")

    if faltan:
        msg = f"⚠ Config incompleta (revisa .env): {', '.join(faltan)}"
        logging.warning(msg)
        print(msg)
        # NO salimos porque quizá solo quieres GASCA o solo modo TG manual.
        # Si quieres obligarlo, cambia a sys.exit(1).


# ================== GASCA ================== #
def hacer_login_gasca(page):
    logging.info("Iniciando login GASCA...")
    print("➡ [GASCA] Yendo a pantalla de login...")
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")

    print("➡ [GASCA] Llenando usuario y contraseña...")
    page.get_by_label("Usuario").fill(USER)
    page.get_by_label("Contraseña").fill(PASS)

    print("➡ [GASCA] Clic en INICIAR SESIÓN...")
    page.get_by_role("button", name="INICIAR SESIÓN").click()
    page.wait_for_load_state("networkidle")

    # Manejar posible 404
    try:
        ir_a_inicio = page.get_by_text("Ir a Inicio")
        if ir_a_inicio.count() > 0:
            print("⚠ [GASCA] Salió 404, clic en 'Ir a Inicio'...")
            ir_a_inicio.first.click()
            page.wait_for_load_state("networkidle")
    except Exception:
        pass

    print("✔ [GASCA] Login OK")


def seleccionar_tipo_reporte(page, texto_opcion: str):
    logging.info(f"Seleccionando '{texto_opcion}' en Tipo de Reporte...")
    print(f"➡ Seleccionando tipo de reporte: {texto_opcion}")

    page.wait_for_selector("select", timeout=15_000)

    timeout_s = 20
    start = time.time()
    ultimo = None

    while time.time() - start < timeout_s:
        result = page.evaluate(
            """
            (labelBuscado) => {
                const selects = Array.from(document.querySelectorAll('select'));
                if (!selects.length) return 'no-selects';

                const sel = selects[0];
                const options = Array.from(sel.options);

                const opt = options.find(o =>
                    o.textContent.trim().toLowerCase() === labelBuscado.trim().toLowerCase()
                );

                if (!opt) return 'no-option';

                sel.value = opt.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return 'ok';
            }
            """,
            texto_opcion,
        )

        ultimo = result
        print(f"   seleccionar_tipo_reporte('{texto_opcion}') => {result}")
        if result == "ok":
            time.sleep(1)
            return
        time.sleep(1)

    raise RuntimeError(f"No se pudo seleccionar '{texto_opcion}'. Último: {ultimo}")


def setear_fecha_corte(page, fecha_objetivo: date):
    fecha_str = fecha_objetivo.strftime("%m/%d/%Y")
    print(f"📅 Seteando Fecha Corte a: {fecha_str}")
    logging.info(f"Seteando Fecha Corte a: {fecha_str}")

    input_locator = page.locator("#txtFechaIn input.form-control").first
    input_locator.wait_for(state="visible", timeout=15_000)

    input_locator.click()
    input_locator.fill("")
    input_locator.type(fecha_str, delay=20)
    input_locator.press("Tab")

    deadline = time.time() + 3
    while time.time() < deadline:
        v = input_locator.input_value().strip()
        if v == fecha_str:
            break
        time.sleep(0.1)

    valor_final = input_locator.input_value().strip()
    if valor_final != fecha_str:
        raise RuntimeError(f"No se pudo setear Fecha Corte. Esperado={fecha_str} Valor={valor_final}")

    page.wait_for_timeout(300)


def click_generar(page):
    print("➡ Clic en 'Generar'...")
    try:
        page.get_by_role("button", name="Generar").click(no_wait_after=True)
        return
    except Exception:
        pass
    try:
        page.locator("button:has-text('Generar')").first.click(no_wait_after=True)
        return
    except Exception:
        pass
    page.get_by_text("Generar", exact=False).first.click()


def esperar_tabla(page):
    timeout_s = 60
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            if page.locator("table").count() > 0:
                return
        except Exception:
            pass
        time.sleep(1)
    raise PlaywrightTimeoutError("No apareció tabla tras Generar (timeout 60s).")


def click_excel(page):
    print("➡ Clic en botón 'Excel'...")
    try:
        btn = page.get_by_role("button", name="Excel")
        if btn.count() > 0:
            btn.first.click(no_wait_after=True)
            return
    except Exception:
        pass

    try:
        page.get_by_text("Excel", exact=False).first.click(no_wait_after=True)
        return
    except Exception:
        pass

    try:
        page.locator("a.btn-success:has-text('Excel'), button.btn-success:has-text('Excel')").first.click(no_wait_after=True)
        return
    except Exception:
        pass

    raise RuntimeError("No encontré el botón 'Excel' para exportar.")


def ejecutar_con_reintentos(fn, nombre):
    ultimo = None
    for intento in range(1, MAX_RETRIES + 1):
        print(f"\n🔄 {nombre} - intento {intento}/{MAX_RETRIES}\n")
        try:
            return fn()
        except Exception as e:
            ultimo = e
            logging.warning(f"{nombre} falló en intento {intento}: {e}")
            if intento < MAX_RETRIES:
                print("⚠ Falló, reintentando en 5s...")
                time.sleep(5)
    raise RuntimeError(f"{nombre}: falló después de {MAX_RETRIES} intentos. Último error: {ultimo}")


def descargar_excel_ventas_nuevas_socios(page):
    print("➡ [GASCA] Entrando a KPIs...")
    page.goto(KPI_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    seleccionar_tipo_reporte(page, "Ventas Nuevas Socios")

    ayer = datetime.now(TZ).date() - timedelta(days=1)
    setear_fecha_corte(page, ayer)

    click_generar(page)
    esperar_tabla(page)

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dlinfo:
        click_excel(page)

    download = dlinfo.value
    suggested = download.suggested_filename or "ventas_nuevas_socios.xlsx"

    hora = datetime.now(TZ).strftime("%H-%M")
    raw_path = RAW_DIR / f"gasca_ventas_nuevas_socios_{ayer:%Y-%m-%d}_{hora}_{suggested}"
    download.save_as(str(raw_path))
    print(f"✅ [GASCA] Raw descargado: {raw_path.name}")
    logging.info(f"[GASCA] Raw guardado: {raw_path}")

    latest_path = OUT_DIR / "gasca_ventas_nuevas_socios.xlsx"
    try:
        df = pd.read_excel(raw_path)
        df.to_excel(latest_path, index=False)
        print(f"✅ [GASCA] Latest: {latest_path.name} (rows={len(df)})")
    except Exception as e:
        logging.warning(f"[GASCA] No pude leer Excel a DF, copiando raw como latest. Error: {e}")
        raw_path.replace(latest_path)
        print(f"✅ [GASCA] Latest (raw): {latest_path.name}")

    return True


# ================== TRAINING GYM (sesión persistente) ================== #
def tg_context(browser):
    """
    Si existe tg_state.json, lo usa para entrar sin login.
    Si no existe, crea contexto normal para que hagas login manual y luego guardemos.
    """
    if TG_STATE_PATH.exists():
        print(f"🔐 [TG] Usando sesión guardada: {TG_STATE_PATH}")
        return browser.new_context(storage_state=str(TG_STATE_PATH))
    print("🔐 [TG] No hay sesión guardada. Se capturará con login manual.")
    return browser.new_context()


def tg_capture_state(context):
    context.storage_state(path=str(TG_STATE_PATH))
    print(f"✅ [TG] Sesión guardada en: {TG_STATE_PATH}")


def tg_goto_workout_report(page):
    print("➡ [TG] yendo a reporte Rutinas y pesajes...")
    page.goto(TRAINING_REPORT_WORKOUT_URL, timeout=60_000)
    page.wait_for_load_state("networkidle", timeout=90_000)
    print(f"✔ [TG] URL actual: {page.url}")


def tg_ensure_logged_in_with_manual_capture(page, context):
    """
    1) Intenta ir directo al reporte.
    2) Si te manda a /auth, tú haces login manual + seleccionas centro.
    3) Guardamos tg_state.json y volvemos a entrar al reporte.
    """
    tg_goto_workout_report(page)

    if "/auth" in page.url:
        print("\n👉 [TG] Te mandó a /auth.")
        print("👉 Haz LOGIN MANUAL en la ventana:")
        print("   - Usuario/Contraseña")
        print("   - Selecciona centro (Azahares o el que sea)")
        print("   - Entra al sistema")
        print("Cuando ya estés dentro, vuelve aquí y presiona ENTER.\n")

        input("⏸ ENTER para guardar sesión y continuar...")

        tg_capture_state(context)

        # Reintenta entrar directo al reporte ya con sesión
        tg_goto_workout_report(page)

        if "/auth" in page.url:
            raise RuntimeError("[TG] Aún estás en /auth incluso después del login manual. Algo bloqueó la sesión.")


# ================== MAIN ================== #
def patch_stealth(ctx):
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

def main():
    validar_config()

    with sync_playwright() as p:
        # ================= GASCA (opcional) =================
        # Usamos Edge (msedge) pero NO persistente.
        browser = p.chromium.launch(
            channel="msedge",  # o "chrome"
            headless=not SHOW_BROWSER,
            args=["--disable-blink-features=AutomationControlled"],
        )

        try:
            print("\n================ GASCA ================\n")
            context_gasca = browser.new_context(accept_downloads=True)
            patch_stealth(context_gasca)
            page_gasca = context_gasca.new_page()

            hacer_login_gasca(page_gasca)

            # Descomenta cuando quieras activar descarga Gasca
            # ejecutar_con_reintentos(
            #     lambda: descargar_excel_ventas_nuevas_socios(page_gasca),
            #     "GASCA - Ventas Nuevas Socios (Excel)"
            # )

            context_gasca.close()
            print("✅ GASCA OK (si estaba activado)\n")

        except Exception as e:
            print(f"⚠ GASCA falló (no detengo todo): {e}")
            logging.warning(f"GASCA falló: {e}")

        # ================= TRAINING GYM =================
        print("\n================ TRAINING GYM ================\n")

        tg_profile = str((BASE_DIR / ".tg_profile").resolve())

        context_tg = p.chromium.launch_persistent_context(
            tg_profile,
            channel="msedge",  # o "chrome"
            headless=not SHOW_BROWSER,
            args=["--disable-blink-features=AutomationControlled"],
        )
        patch_stealth(context_tg)

        page_tg = context_tg.new_page()

        tg_ensure_logged_in_with_manual_capture(page_tg, context_tg)
        print("✅ [TG] Listo: ya estás dentro y en /reports/workout")

        if SHOW_BROWSER:
            input("⏸ ENTER para cerrar navegador...")

        context_tg.close()
        browser.close()


if __name__ == "__main__":
    main()
