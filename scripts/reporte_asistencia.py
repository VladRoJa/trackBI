import os
import sys
import logging
import time
from datetime import datetime, date
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import pytz
from datetime import timedelta

# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]  # TRACK BI
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "reporte_asistencia.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

USER = os.getenv("DIRECCION_USER")
PASS = os.getenv("DIRECCION_PASS")
LOGIN_URL = os.getenv("DIRECCION_LOGIN_URL")
REPORTES_URL = os.getenv("REPORTES_URL")

SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"

TZ = pytz.timezone("America/Tijuana")

OUTPUT_DIR = BASE_DIR / "data" / "asistencia"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_RETRIES = 3


# ============================================================
# VALIDACIÓN
# ============================================================

def validar_config():
    faltan = []
    if not USER: faltan.append("DIRECCION_USER")
    if not PASS: faltan.append("DIRECCION_PASS")
    if not LOGIN_URL: faltan.append("DIRECCION_LOGIN_URL")
    if not REPORTES_URL: faltan.append("REPORTES_URL")

    if faltan:
        msg = f"Faltan variables en .env: {', '.join(faltan)}"
        logging.error(msg)
        print(msg)
        sys.exit(1)


# ============================================================
# HELPERS
# ============================================================

def limpiar_excel_inplace(ruta: Path | str):
    """
    Lee un .xlsx y lo vuelve a guardar sin estilos/formatos, sólo datos.
    Si no se puede leer, deja el archivo tal cual y NO truena.
    """
    ruta = Path(ruta)
    print(f"🧹 Limpiando Excel: {ruta.name}...", flush=True)
    logging.info(f"Limpiando Excel: {ruta}")

    df = None
    ultimo_error = None
    for intento in range(1, 4):
        try:
            df = pd.read_excel(ruta)
            break
        except Exception as e:
            ultimo_error = e
            logging.warning(f"No se pudo leer {ruta} intento {intento}/3: {e}")
            print(f"⚠ No se pudo leer {ruta.name} (intento {intento}/3). Esperando 10s...", flush=True)
            time.sleep(10)

    if df is None:
        msg = f"No se pudo limpiar {ruta.name}. Se deja sin cambios. Error: {ultimo_error}"
        logging.error(msg)
        print(f"⚠ {msg}", flush=True)
        return ruta

    tmp_path = ruta.with_suffix(".tmp.xlsx")
    df.to_excel(tmp_path, index=False)

    try:
        ruta.unlink()
    except Exception as e:
        logging.warning(f"No se pudo borrar original {ruta}: {e}")

    tmp_path.rename(ruta)

    print(f"✔ Excel limpio guardado: {ruta.name}", flush=True)
    logging.info(f"Excel limpio guardado: {ruta}")
    return ruta


def hacer_login(page):
    logging.info("Iniciando login en Gasca...")
    print("➡ Yendo a pantalla de login...", flush=True)

    page.goto(LOGIN_URL, timeout=60_000)

    print("➡ Llenando usuario y contraseña...", flush=True)
    page.get_by_label("Usuario").fill(USER)
    page.get_by_label("Contraseña").fill(PASS)

    print("➡ Clic en INICIAR SESIÓN...", flush=True)
    page.get_by_role("button", name="INICIAR SESIÓN").click()
    page.wait_for_load_state("networkidle")

    # posible 404 con "Ir a Inicio"
    try:
        ir_a_inicio = page.get_by_text("Ir a Inicio")
        if ir_a_inicio.count() > 0:
            print("⚠ Salió 404, clic en 'Ir a Inicio'...", flush=True)
            ir_a_inicio.first.click()
            page.wait_for_load_state("networkidle")
    except Exception:
        pass

    print("✅ Login OK", flush=True)


def seleccionar_tipo_reporte(page, texto_opcion: str):
    """
    Selecciona el tipo de reporte usando JS directo (igual a tus scripts).
    """
    logging.info(f"Seleccionando tipo de reporte '{texto_opcion}'...")
    print(f"➡ Seleccionando tipo de reporte '{texto_opcion}'...", flush=True)

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
        print(f"   intento seleccionar_tipo_reporte('{texto_opcion}') => {result}", flush=True)
        if result == "ok":
            time.sleep(1)
            return
        time.sleep(1)

    raise RuntimeError(f"No se pudo seleccionar '{texto_opcion}' en {timeout_s}s. Último: {ultimo}")


def click_boton_generar(page):
    print("➡ Buscando botón 'Generar'...", flush=True)
    try:
        page.get_by_role("button", name="Generar").click()
        print("✔ Click en 'Generar' (get_by_role).", flush=True)
        return
    except Exception:
        pass

    try:
        page.locator("button:has-text('Generar')").first.click()
        print("✔ Click en 'Generar' (button:has-text).", flush=True)
        return
    except Exception as e:
        raise RuntimeError(f"No se pudo hacer clic en 'Generar': {e}")


def esperar_loader_asistencias(page, timeout: int = 180):
    """
    Loader (overlay) con texto "Reporte Estadisticas De Asistencias..."
    Si no aparece, seguimos.
    """
    print("⏳ Esperando loader de Asistencias...", flush=True)
    try:
        page.wait_for_selector("text=Reporte Estadisticas De Asistencias", timeout=10_000)
        page.wait_for_selector(
            "text=Reporte Estadisticas De Asistencias",
            state="detached",
            timeout=timeout * 1000
        )
        print("✔ Loader terminó.", flush=True)
    except Exception:
        print("⚠ Loader no detectado (probablemente cargó rápido).", flush=True)


def esperar_min_filas_tabla(page, min_filas: int = 10, timeout: int = 180):
    print(f"⏳ Esperando >= {min_filas} filas en la tabla...", flush=True)
    start = time.time()
    ultimo = 0
    while time.time() - start < timeout:
        try:
            filas = page.locator("table tbody tr")
            cnt = filas.count()
            ultimo = cnt
            if cnt >= min_filas:
                print(f"✔ Tabla lista ({cnt} filas).", flush=True)
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"La tabla no llegó a {min_filas} filas en {timeout}s (último={ultimo}).")


def exportar_excel(page, destino: Path):
    """
    Click Exportar -> Excel y guarda el archivo.
    """
    print("➡ Exportando a Excel...", flush=True)

    # Botón Exportar
    try:
        export_btn = page.get_by_role("button", name="Exportar")
    except Exception:
        export_btn = page.locator("button:has-text('Exportar')").first

    export_btn.scroll_into_view_if_needed()
    export_btn.click()
    time.sleep(1)

    # Click Excel con expect_download
    try:
        with page.expect_download(timeout=60_000) as dl_info:
            try:
                page.get_by_text("Excel", exact=False).first.click()
            except Exception:
                page.locator("text=Excel").first.click()
        download = dl_info.value
    except PlaywrightTimeoutError:
        raise RuntimeError("No se pudo iniciar/terminar la descarga de Excel en 60s.")

    if destino.exists():
        destino.unlink()

    download.save_as(str(destino))
    print(f"✅ Guardado: {destino}", flush=True)
    logging.info(f"Excel guardado en: {destino}")
    return destino


# ============================================================
# REPORTE
# ============================================================

def descargar_asistencias(page):
    """
    Reporte De Estadisticas De Asistencias:
    - Fechas = AYER (inicio y fin)
    - Generar
    - Esperar loader + >=10 filas
    - Exportar Excel
    - Guardar por día
    """
    print("\n🔹 Descargando 'Reporte De Estadisticas De Asistencias' (AYER)...\n", flush=True)
    logging.info("==== Descarga: Reporte De Estadisticas De Asistencias ====")

    page.goto(REPORTES_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    seleccionar_tipo_reporte(page, "Reporte De Estadisticas De Asistencias")

    # AYER TZ Tijuana
    ayer = datetime.now(TZ).date() - timedelta(days=1)
    fecha_str = ayer.strftime("%m/%d/%Y")

    inputs = page.locator("input[type='text']")
    if inputs.count() < 2:
        raise RuntimeError(f"Asistencias: esperaba >=2 inputs para fechas, pero encontré {inputs.count()}")

    # Fecha Inicio y Fin
    for idx in [0, 1]:
        campo = inputs.nth(idx)
        campo.click()
        campo.fill("")
        campo.type(fecha_str, delay=50)
        time.sleep(0.2)

    click_boton_generar(page)

    esperar_loader_asistencias(page, timeout=180)
    esperar_min_filas_tabla(page, min_filas=10, timeout=180)

    nombre_archivo = f"asistencias_{ayer:%Y-%m-%d}.xlsx"
    destino = OUTPUT_DIR / nombre_archivo

    ruta = exportar_excel(page, destino)
    ruta = limpiar_excel_inplace(ruta)

    return ruta


def ejecutar_con_reintentos(fn, nombre):
    ultimo = None
    for intento in range(1, MAX_RETRIES + 1):
        print(f"\n🔄 {nombre} - intento {intento}/{MAX_RETRIES}\n", flush=True)
        try:
            return fn()
        except Exception as e:
            ultimo = e
            logging.warning(f"{nombre} falló en intento {intento}: {e}")
            if intento < MAX_RETRIES:
                print(f"⚠ Falló, reintentando en 5s... ({e})", flush=True)
                time.sleep(5)
    raise RuntimeError(f"{nombre} falló después de {MAX_RETRIES} intentos. Último error: {ultimo}")


# ============================================================
# MAIN
# ============================================================

def main():
    validar_config()
    print("🚀 Iniciando reporte_asistencia.py", flush=True)
    logging.info("==== Inicio de ejecución reporte_asistencia.py ====")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not SHOW_BROWSER)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            page.set_default_timeout(120_000)
            page.set_default_navigation_timeout(120_000)

            hacer_login(page)

            ejecutar_con_reintentos(lambda: descargar_asistencias(page), "Reporte Asistencias")

            browser.close()

    except Exception as e:
        msg = f"❌ Error general en reporte_asistencia.py: {e}"
        print(msg, flush=True)
        logging.error(msg)
        sys.exit(1)

    print("🎉 reporte_asistencia.py terminado OK", flush=True)


if __name__ == "__main__":
    main()
