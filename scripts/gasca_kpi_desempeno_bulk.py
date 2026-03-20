"""
gasca_kpi_desempeno_rango.py
----------------------------
Descarga el KPI "Desempeño" de Gasca por rango de fechas.
Flujo por día:
  - Set fecha
  - Click Generar
  - Esperar que termine loader/processing y que la tabla tenga filas
  - Click Exportar -> Excel
  - Esperar descarga y guardar en data/desempeno/

Requisitos:
- Variables en .env (ya las tienes):
  DIRECCION_USER, DIRECCION_PASS, DIRECCION_LOGIN_URL
- Navega después de login a:
  https://ultragimnasios.com/Modulo/Kpis/Index  (KPI_URL)

Ejecución:
  python scripts/gasca_kpi_desempeno_rango.py

Opcional:
  SHOW_BROWSER=1 para verlo (en .env)
"""

import os
import time
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TZ = pytz.timezone("America/Tijuana")

BASE_DIR = Path(__file__).resolve().parents[1]  # TRACK BI
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "gasca_kpi_desempeno_rango.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

USER = os.getenv("DIRECCION_USER")
PASS = os.getenv("DIRECCION_PASS")
LOGIN_URL = os.getenv("DIRECCION_LOGIN_URL", "https://ultragimnasios.com/")
KPI_URL = os.getenv("KPI_DESEMPENO_URL", "https://ultragimnasios.com/Modulo/Kpis/Index")

SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"

OUT_DIR = (BASE_DIR / "data" / "desempeno").resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_TIMEOUT_MS = 180_000


def fmt_mmddyyyy(d: date) -> str:
    # Tu datepicker trabaja en MM/DD/YYYY
    return d.strftime("%m/%d/%Y")


def asegurar_config():
    faltan = []
    if not USER:
        faltan.append("DIRECCION_USER")
    if not PASS:
        faltan.append("DIRECCION_PASS")
    if not LOGIN_URL:
        faltan.append("DIRECCION_LOGIN_URL")
    if faltan:
        raise RuntimeError(f"Faltan variables en .env: {', '.join(faltan)}")


def hacer_login(page):
    print("➡ [GASCA] Login...")
    page.goto(LOGIN_URL, timeout=90_000)
    page.wait_for_load_state("domcontentloaded")

    # Ajusta si tus labels cambian
    page.get_by_label("Usuario").fill(USER)
    page.get_by_label("Contraseña").fill(PASS)
    page.get_by_role("button", name="INICIAR SESIÓN").click()
    page.wait_for_load_state("networkidle", timeout=90_000)

    # Por si aparece el 404 con "Ir a Inicio"
    try:
        ir = page.get_by_text("Ir a Inicio", exact=False)
        if ir.count() > 0:
            ir.first.click()
            page.wait_for_load_state("networkidle", timeout=90_000)
    except Exception:
        pass

    print("✅ [GASCA] Login OK")


def seleccionar_tipo_reporte_desempeno(page):
    # En tu pantalla es un <select> arriba que dice "Tipo de Reporte"
    page.wait_for_selector("select", timeout=20_000)

    ok = page.evaluate(
        """
        () => {
          const selects = Array.from(document.querySelectorAll("select"));
          if (!selects.length) return "no-select";
          const sel = selects[0];
          const opts = Array.from(sel.options);
          const opt = opts.find(o => (o.textContent || "").trim().toLowerCase() === "desempeño");
          if (!opt) return "no-option";
          sel.value = opt.value;
          sel.dispatchEvent(new Event("change", {bubbles:true}));
          return "ok";
        }
        """
    )
    if ok != "ok":
        raise RuntimeError(f"No pude seleccionar 'Desempeño' en Tipo de Reporte. result={ok}")

    page.wait_for_timeout(600)


def setear_fecha(page, d: date):
    fecha_str = fmt_mmddyyyy(d)
    print(f"📅 [GASCA] Fecha corte => {fecha_str}")

    inp = page.locator("#txtFechaIn input.form-control").first
    inp.wait_for(state="visible", timeout=20_000)

    inp.click()
    inp.fill("")
    inp.type(fecha_str, delay=15)
    inp.press("Tab")

    # Confirmar que quedó
    deadline = time.time() + 4
    while time.time() < deadline:
        v = (inp.input_value() or "").strip()
        if v == fecha_str:
            return
        page.wait_for_timeout(150)

    raise RuntimeError(f"No se pudo setear fecha. expected={fecha_str}, got={(inp.input_value() or '').strip()}")


def click_generar(page):
    btn = page.get_by_role("button", name="Generar")
    if btn.count() == 0:
        btn = page.locator("button:has-text('Generar')").first

    btn.wait_for(state="visible", timeout=20_000)
    btn.click(no_wait_after=True)
    page.wait_for_timeout(250)


def esperar_tabla_lista(page, timeout_ms=120_000):
    """
    Espera:
    - que termine cualquier processing/loader típico de DataTables
    - que haya filas reales en la tabla
    """
    deadline = time.time() + timeout_ms / 1000

    processing_selectors = [
        ".dataTables_processing",
        "div.processing",
        "div.loader",
        "div.loading",
        "div:has-text('Procesando')",
        "div:has-text('Cargando')",
    ]

    # tabla principal (fallback genérico)
    tbody_rows = page.locator("table tbody tr")

    # Espera a que aparezcan filas "reales"
    while time.time() < deadline:
        # 1) Si hay processing visible, espera a que se quite
        processing_visible = False
        for sel in processing_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    processing_visible = True
                    break
            except Exception:
                pass

        if processing_visible:
            page.wait_for_timeout(400)
            continue

        # 2) filas
        try:
            c = tbody_rows.count()
            if c > 0:
                # evitar caso "No data available" / "No se encontraron"
                first_text = (tbody_rows.first.inner_text() or "").strip().lower()
                if "no data" in first_text or "no se" in first_text or "sin" in first_text:
                    page.wait_for_timeout(600)
                    continue
                return True
        except Exception:
            pass

        page.wait_for_timeout(500)

    raise PlaywrightTimeoutError("Timeout esperando tabla lista (sin loader y con filas).")


def exportar_excel(page, target_path: Path):
    """
    Click Exportar -> Excel y espera descarga
    """
    # Botón Exportar (izquierda arriba de la tabla)
    export_btn = page.get_by_role("button", name="Exportar")
    if export_btn.count() == 0:
        export_btn = page.locator("button:has-text('Exportar')").first

    export_btn.wait_for(state="visible", timeout=20_000)
    export_btn.click(no_wait_after=True)
    page.wait_for_timeout(250)

    # Opción Excel (suele ser link o botón dentro de un dropdown)
    excel = page.locator("a:has-text('Excel'), button:has-text('Excel'), li:has-text('Excel')").first
    excel.wait_for(state="visible", timeout=20_000)

    print(f"⬇️ [GASCA] Descargando => {target_path.name}")
    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dlinfo:
        excel.click(no_wait_after=True)

    download = dlinfo.value
    download.save_as(str(target_path))
    print(f"✅ [GASCA] OK => {target_path.name}")


def descargar_por_dia(page, d: date, retries=3):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            print(f"\n=== {d:%Y-%m-%d} intento {attempt}/{retries} ===")
            page.goto(KPI_URL, timeout=120_000)
            page.wait_for_load_state("networkidle", timeout=120_000)

            seleccionar_tipo_reporte_desempeno(page)
            setear_fecha(page, d)
            click_generar(page)
            esperar_tabla_lista(page, timeout_ms=180_000)

            out = OUT_DIR / f"kpi_desempeno_{d:%Y-%m-%d}.xlsx"
            exportar_excel(page, out)
            return True

        except Exception as e:
            last_err = e
            logging.warning(f"{d:%Y-%m-%d} fallo intento {attempt}: {e}")
            print(f"⚠ [GASCA] Falló {d:%Y-%m-%d} intento {attempt}: {e}")

            # mini refresh antes de reintentar
            try:
                page.reload(wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1200)
            except Exception:
                pass

            time.sleep(2)

    raise RuntimeError(f"Falló {d:%Y-%m-%d} tras {retries} intentos. last_err={last_err}")


def main():
    asegurar_config()

    # RANGO: 01/01 al 08/03 (ajusta el año aquí)
    year = 2026  # <-- CAMBIA AQUÍ si lo quieres para otro año
    start = date(year, 3, 1)
    end = date(year, 3, 10)

    print("======================================")
    print(" GASCA KPI DESEMPEÑO - DESCARGA RANGO ")
    print("======================================")
    print(f"Rango: {start:%Y-%m-%d} -> {end:%Y-%m-%d}")
    print(f"Salida: {OUT_DIR}")
    print("")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not SHOW_BROWSER)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.set_default_timeout(30_000)
        page.set_default_navigation_timeout(120_000)

        try:
            hacer_login(page)

            d = start
            while d <= end:
                descargar_por_dia(page, d, retries=3)
                d += timedelta(days=1)

            print("\n✅ Terminé el rango completo.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()