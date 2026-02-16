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
    filename=LOGS_DIR / "socios_nuevos_gasca.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

USER      = os.getenv("DIRECCION_USER")
PASS      = os.getenv("DIRECCION_PASS")
LOGIN_URL = os.getenv("DIRECCION_LOGIN_URL")

# Módulo KPIs (Gasca)
KPI_URL = os.getenv("KPI_DESEMPENO_URL", "https://ultragimnasios.com/Modulo/Kpis/Index")

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
    if faltan:
        msg = f"Faltan variables en .env: {', '.join(faltan)}"
        logging.error(msg)
        print("❌", msg)
        sys.exit(1)


# ================== Login GASCA ================== #
def hacer_login_gasca(page):
    logging.info("Iniciando login GASCA...")
    print("➡ [GASCA] Abriendo login...")
    page.goto(LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")

    print("➡ [GASCA] Llenando credenciales...")
    page.get_by_label("Usuario").fill(USER)
    page.get_by_label("Contraseña").fill(PASS)

    print("➡ [GASCA] Click INICIAR SESIÓN...")
    page.get_by_role("button", name="INICIAR SESIÓN").click()
    page.wait_for_load_state("networkidle")

    # Manejar posible 404 / pantalla intermedia
    try:
        ir_a_inicio = page.get_by_text("Ir a Inicio")
        if ir_a_inicio.count() > 0 and ir_a_inicio.first.is_visible():
            print("⚠ [GASCA] Salió 404, clic en 'Ir a Inicio'...")
            ir_a_inicio.first.click()
            page.wait_for_load_state("networkidle")
    except Exception:
        pass

    print("✔ [GASCA] Login OK")


# ================== Helpers UI ================== #
def seleccionar_tipo_reporte(page, texto_opcion: str):
    """
    Selecciona opción del combo 'Tipo de Reporte' (primer <select> del módulo).
    """
    logging.info(f"Seleccionando '{texto_opcion}' en Tipo de Reporte...")
    print(f"➡ [GASCA] Tipo de reporte: {texto_opcion}")

    page.wait_for_selector("select", timeout=15_000)

    timeout_s = 25
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
    """
    Setea el datepicker 'Fecha Corte' (#txtFechaIn input.form-control) al formato MM/DD/YYYY.
    """
    fecha_str = fecha_objetivo.strftime("%m/%d/%Y")
    print(f"📅 [GASCA] Fecha Corte: {fecha_str}")
    logging.info(f"Seteando Fecha Corte a: {fecha_str}")

    input_locator = page.locator("#txtFechaIn input.form-control").first
    input_locator.wait_for(state="visible", timeout=15_000)

    input_locator.click()
    input_locator.fill("")
    input_locator.type(fecha_str, delay=20)
    input_locator.press("Tab")

    # Verificar que quedó (a veces el plugin tarda)
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
    print("➡ [GASCA] Click 'Generar'...")
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
    """
    Espera hasta 60s a que exista una tabla (la inferior con resultados).
    """
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
    """
    Botón verde 'Excel' (a la izquierda de Generar).
    """
    print("➡ [GASCA] Click botón 'Excel'...")
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


# ================== Descarga: Ventas Nuevas Socios ================== #
def descargar_excel_ventas_nuevas_socios(page):
    print("➡ [GASCA] Entrando a KPIs...")
    page.goto(KPI_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    # 1) Tipo de reporte
    seleccionar_tipo_reporte(page, "Ventas Nuevas Socios")

    # 2) Fecha Corte = ayer (Tijuana)
    ayer = datetime.now(TZ).date() - timedelta(days=1)
    setear_fecha_corte(page, ayer)

    # 3) Generar
    click_generar(page)

    # 4) Esperar tabla (para asegurar que ya generó)
    esperar_tabla(page)

    # 5) Descargar Excel
    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dlinfo:
        click_excel(page)

    download = dlinfo.value
    suggested = download.suggested_filename or "ventas_nuevas_socios.xlsx"

    hora = datetime.now(TZ).strftime("%H-%M")
    raw_path = RAW_DIR / f"gasca_ventas_nuevas_socios_{ayer:%Y-%m-%d}_{hora}_{suggested}"
    download.save_as(str(raw_path))
    print(f"✅ [GASCA] Raw descargado: {raw_path.name}")
    logging.info(f"[GASCA] Raw guardado: {raw_path}")

    # 6) Normalizar a un “latest” (sobrescribe)
    latest_path = OUT_DIR / "gasca_ventas_nuevas_socios.xlsx"
    try:
        df = pd.read_excel(raw_path)
        df.to_excel(latest_path, index=False)
        print(f"✅ [GASCA] Latest: {latest_path.name} (rows={len(df)})")
    except Exception as e:
        logging.warning(f"[GASCA] No pude leer Excel a DF, copiando raw como latest. Error: {e}")
        try:
            if latest_path.exists():
                latest_path.unlink()
            raw_path.replace(latest_path)
        except Exception:
            pass
        print(f"✅ [GASCA] Latest (raw): {latest_path.name}")

    return True


# ================== MAIN ================== #
def main():
    validar_config()
    logging.info("==== Inicio: GASCA Ventas Nuevas Socios (Excel) ====")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge",  # o "chrome"
            headless=not SHOW_BROWSER,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        hacer_login_gasca(page)

        ejecutar_con_reintentos(
            lambda: descargar_excel_ventas_nuevas_socios(page),
            "GASCA - Ventas Nuevas Socios (Excel)"
        )

        context.close()
        browser.close()

    print("✅ GASCA OK")


if __name__ == "__main__":
    main()
