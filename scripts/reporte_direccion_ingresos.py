import os
import sys
import logging
import time
from datetime import datetime
from pathlib import Path
from io import StringIO  # para evitar FutureWarning de read_html

from dotenv import load_dotenv
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
import pandas as pd
import requests

# ================= Configuración general ================= #

BASE_DIR = Path(__file__).resolve().parents[1]   # carpeta TRACK BI
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "reporte_direccion_ingresos.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

USER        = os.getenv("DIRECCION_USER")
PASS        = os.getenv("DIRECCION_PASS")
LOGIN_URL   = os.getenv("DIRECCION_LOGIN_URL")
REPORTE_URL = os.getenv("DIRECCION_REPORTE_URL")

# Reporte Dirección
OUTPUT_DIR  = Path(os.getenv("DIRECCION_OUTPUT_DIR", BASE_DIR / "data/direccion_ingresos")).resolve()

# KPIs (misma URL, distintos tipos de reporte)
KPI_URL = os.getenv("KPI_DESEMPENO_URL", "https://ultragimnasios.com/Modulo/Kpis/Index")
KPI_OUTPUT_DIR = Path(os.getenv("KPI_DESEMPENO_OUTPUT_DIR", BASE_DIR / "data/kpi_desempeno")).resolve()

# KPI Ventas Nuevos Socios
KPI_VENTAS_NS_OUTPUT_DIR = Path(
    os.getenv(
        "KPI_VENTAS_NUEVOS_SOCIOS_OUTPUT_DIR",
        BASE_DIR / "data/kpi_ventas_nuevos_socios"
    )
).resolve()

# Mostrar navegador (1 = visible, 0 = headless)
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"

MAX_RETRIES          = 3
TABLE_WAIT_TIMEOUTMS = 120_000  # 120 segundos

# ==== WhatsApp (opcional, via proveedor externo) ==== #

WA_ENABLED = os.getenv("WA_ENABLED", "0") == "1"
WA_URL     = os.getenv("WA_URL")
WA_TOKEN   = os.getenv("WA_TOKEN")
WA_TO      = os.getenv("WA_TO")


def send_whatsapp(msg: str):
    if not WA_ENABLED:
        return
    if not WA_URL or not WA_TOKEN or not WA_TO:
        logging.warning("WA_ENABLED=1 pero faltan WA_URL / WA_TOKEN / WA_TO")
        return

    try:
        payload = {"to": WA_TO, "message": msg, "token": WA_TOKEN}
        resp = requests.post(WA_URL, json=payload, timeout=15)
        logging.info(f"WhatsApp resp {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logging.error(f"Error enviando WhatsApp: {e}")


def validar_config():
    faltan = []
    if not USER:        faltan.append("DIRECCION_USER")
    if not PASS:        faltan.append("DIRECCION_PASS")
    if not LOGIN_URL:   faltan.append("DIRECCION_LOGIN_URL")
    if not REPORTE_URL: faltan.append("DIRECCION_REPORTE_URL")

    if faltan:
        msg = f"Faltan variables en .env: {', '.join(faltan)}"
        logging.error(msg)
        print(msg)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    KPI_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    KPI_VENTAS_NS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============== utilidades de tablas ============== #

### CAMBIO 1: helper para aplanar columnas MultiIndex usando SOLO la fila de abajo
def aplanar_columnas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Si las columnas son MultiIndex (por encabezados mergeados),
    usa solo la última parte de cada tupla (la fila de encabezado inferior).
    Ejemplo: ('Meta CNM', 'Numero de CNM (Meta)') -> 'Numero de CNM (Meta)'
    """
    if isinstance(df.columns, pd.MultiIndex):
        new_cols = []
        for col in df.columns:
            # col es una tupla, nos quedamos con la última parte no vacía
            partes = [str(x) for x in col if pd.notna(x) and str(x) != "nan"]
            if partes:
                name = partes[-1].strip()
            else:
                name = "col_sin_nombre"
            new_cols.append(name)
        df.columns = new_cols
    return df


def esperar_tabla(page):
    """
    Espera hasta TABLE_WAIT_TIMEOUTMS a que aparezca al menos una tabla.
    Muestra progreso cada 5 segundos para evitar sensación de cuelgue.
    """
    logging.info(f"Esperando tabla hasta {TABLE_WAIT_TIMEOUTMS/1000:.0f} segundos...")

    max_seconds = TABLE_WAIT_TIMEOUTMS // 1000
    start_time = time.time()

    for i in range(max_seconds):
        try:
            if page.locator("table").count() > 0:
                print(f"\n✔ Al menos una tabla detectada después de {i} segundos")
                logging.info(f"Se detectó al menos una tabla después de {i} segundos.")
                return
        except Exception:
            pass

        if i % 5 == 0:
            print(f"⏳ Esperando tabla… {i}/{max_seconds} segundos", flush=True)

        time.sleep(1)

    elapsed = time.time() - start_time
    logging.error(f"No apareció ninguna tabla tras {elapsed:.1f} segundos")
    raise PlaywrightTimeoutError(f"No apareció ninguna tabla en {elapsed:.1f} segundos")


def extraer_tabla_html(page):
    """
    Recorre todas las tablas de la página y devuelve la más grande
    (en número de celdas = filas * columnas). Luego aplana columnas.
    """
    tables = page.locator("table")
    total = tables.count()

    if total == 0:
        raise RuntimeError("No se encontró ninguna tabla en la página.")

    logging.info(f"Se encontraron {total} tablas en la página. Buscando la más grande...")
    best_df = None
    best_score = 0

    for i in range(total):
        try:
            html_table = tables.nth(i).evaluate("el => el.outerHTML")
            df_list = pd.read_html(StringIO(html_table))
        except Exception as e:
            logging.warning(f"Error leyendo tabla {i}: {e}")
            continue

        for df in df_list:
            if df is None or df.empty:
                continue

            df = aplanar_columnas(df)  # <- CAMBIO 2: aplanar aquí también

            filas, columnas = df.shape
            score = filas * columnas
            logging.info(f"Tabla {i} candidata: {filas} filas x {columnas} cols (score={score})")
            if score > best_score:
                best_score = score
                best_df = df

    if best_df is None or best_df.empty:
        raise RuntimeError("No se pudo determinar una tabla principal (todas vacías o con error).")

    logging.info(f"Tabla seleccionada: shape={best_df.shape}")
    return best_df


def click_boton_generar(page):
    """
    Intenta hacer clic en el botón azul 'Generar' usando varios métodos.
    Lanza error si no lo logra.
    """
    logging.info("Haciendo clic en botón 'Generar'...")
    print("➡ Buscando botón 'Generar'...")

    # Intento 1: botón por rol y nombre accesible
    try:
        btn = page.get_by_role("button", name="Generar")
        btn.click(no_wait_after=True)  # CAMBIO 3: no_wait_after para que no se quede colgado
        print("✔ Click en 'Generar' (get_by_role).")
        return
    except Exception as e:
        logging.warning(f"No se pudo cliclear 'Generar' por get_by_role: {e}")

    # Intento 2: botón con texto 'Generar'
    try:
        btn = page.locator("button:has-text('Generar')").first
        btn.click(no_wait_after=True)
        print("✔ Click en 'Generar' (button:has-text).")
        return
    except Exception as e:
        logging.warning(f"No se pudo cliclear 'Generar' por button:has-text: {e}")

    # Intento 3: cualquier elemento con texto 'Generar'
    try:
        page.get_by_text("Generar", exact=False).first.click()
        print("✔ Click en 'Generar' (get_by_text).")
        return
    except Exception as e:
        logging.error(f"No se pudo cliclear 'Generar' en ningún método: {e}")
        raise RuntimeError("No se pudo hacer clic en el botón 'Generar'.")


def obtener_tabla_con_sucursal(page, nombre_reporte: str):
    """
    Escanea todas las tablas de la página durante TABLE_WAIT_TIMEOUTMS
    y devuelve la primera que tenga una columna llamada 'Sucursal'.
    Ignora tablitas como la de atajos de teclado.
    """
    timeout_s = TABLE_WAIT_TIMEOUTMS / 1000.0
    start = time.time()
    print(f"⏳ Buscando tabla principal para {nombre_reporte} (columna 'Sucursal') "
          f"hasta {timeout_s:.0f} segundos...")

    while time.time() - start < timeout_s:
        tablas = page.locator("table")
        total = tablas.count()

        if total == 0:
            time.sleep(1)
            continue

        for i in range(total):
            try:
                html_table = tablas.nth(i).evaluate("el => el.outerHTML")
                df_list = pd.read_html(StringIO(html_table))
            except Exception as e:
                logging.warning(f"{nombre_reporte}: error leyendo tabla {i}: {e}")
                continue

            for df in df_list:
                if df is None or df.empty:
                    continue

                # CAMBIO 4: aplanar columnas AQUÍ también (usa solo la fila de abajo)
                df = aplanar_columnas(df)

                cols_lower = [str(c).strip().lower() for c in df.columns]
                if any("sucursal" in c for c in cols_lower):
                    filas, columnas = df.shape
                    logging.info(
                        f"{nombre_reporte}: tabla con 'Sucursal' encontrada "
                        f"(tabla {i}) {filas} filas x {columnas} columnas."
                    )
                    print(f"✔ Tabla principal de {nombre_reporte} encontrada "
                          f"({filas} filas, {columnas} columnas)")
                    return df

        time.sleep(1)

    raise RuntimeError(
        f"No se encontró una tabla con columna 'Sucursal' para {nombre_reporte} "
        f"dentro de {timeout_s:.0f} segundos."
    )


# ================== Login ================== #

def hacer_login(page):
    """
    Hace login una sola vez y maneja posible 404.
    """
    logging.info("Iniciando login...")
    print("➡ Yendo a pantalla de login...")
    page.goto(LOGIN_URL, timeout=60_000)

    print("➡ Llenando usuario y contraseña...")
    page.get_by_label("Usuario").fill(USER)
    page.get_by_label("Contraseña").fill(PASS)
    print("➡ Clic en INICIAR SESIÓN...")
    page.get_by_role("button", name="INICIAR SESIÓN").click()
    page.wait_for_load_state("networkidle")
    print("✔ Login completado. Verificando posible 404...")

    # Manejar posible 404
    try:
        ir_a_inicio = page.get_by_text("Ir a Inicio")
        if ir_a_inicio.count() > 0:
            logging.info("Detectado 404 tras login. Clic en 'Ir a Inicio'.")
            print("⚠ Salió 404, clic en 'Ir a Inicio'...")
            ir_a_inicio.first.click()
            page.wait_for_load_state("networkidle")
    except Exception:
        pass


# ================== Reporte Dirección ================== #

def descargar_reporte_direccion(page):
    logging.info(f"Navegando a REPORTE_URL: {REPORTE_URL}")
    print("➡ Entrando a Reporte Dirección...")
    page.goto(REPORTE_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    esperar_tabla(page)
    df_dir = extraer_tabla_html(page)

    logging.info(
        f"Reporte Dirección: tabla extraída con {len(df_dir)} filas y {len(df_dir.columns)} columnas."
    )
    print("✔ Reporte Dirección descargado.")
    return df_dir


# ================== KPIs ================== #

def seleccionar_tipo_reporte(page, texto_opcion: str):
    """
    Selecciona una opción del combo 'Tipo de Reporte' usando JS directo.
    texto_opcion: texto visible (ej. 'Desempeño', 'Ventas Nuevas Socios').
    Reintenta varios segundos por si el frontend tarda en llenar el combo.
    """
    logging.info(f"Seleccionando '{texto_opcion}' en Tipo de Reporte mediante JS...")
    print(f"➡ Buscando combo 'Tipo de Reporte' para '{texto_opcion}'...")

    # Espera a que exista al menos un <select> en la página
    page.wait_for_selector("select", timeout=15_000)

    timeout_s = 20
    start = time.time()
    ultimo_result = None

    while time.time() - start < timeout_s:
        result = page.evaluate(
            """
            (labelBuscado) => {
                const selects = Array.from(document.querySelectorAll('select'));
                if (!selects.length) return 'no-selects';

                // En esta pantalla solo hay 1 combo principal para 'Tipo de Reporte'
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

        ultimo_result = result
        print(f"   intento seleccionar_tipo_reporte('{texto_opcion}') => {result}")

        if result == "ok":
            print(f"✔ '{texto_opcion}' seleccionado en Tipo de Reporte.")
            logging.info(f"'{texto_opcion}' seleccionado correctamente en Tipo de Reporte.")
            time.sleep(1)  # pequeña pausa para que el frontend procese el change
            return

        time.sleep(1)

    raise RuntimeError(
        f"No se pudo seleccionar '{texto_opcion}' en 'Tipo de Reporte' "
        f"después de {timeout_s}s. Último resultado: {ultimo_result}"
    )


def descargar_kpi_desempeno(page):
    """
    Asume que ya estás logueado.
    Va a la página de KPIs, selecciona 'Desempeño', genera y devuelve el DataFrame.
    """
    logging.info(f"Navegando a KPI_URL: {KPI_URL}")
    print("➡ Entrando a KPIs (Desempeño)...")
    page.goto(KPI_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    # Seleccionar tipo de reporte: Desempeño
    seleccionar_tipo_reporte(page, "Desempeño")

    # Clic en Generar
    click_boton_generar(page)

    # Esperar tabla con 'Sucursal'
    df_kpi = obtener_tabla_con_sucursal(page, "KPI Desempeño")

    logging.info(
        f"KPI Desempeño: tabla extraída con {len(df_kpi)} filas y {len(df_kpi.columns)} columnas."
    )
    print("✔ KPI Desempeño descargado.")
    return df_kpi


def descargar_kpi_ventas_nuevos_socios(page):
    """
    Asume que ya estás logueado.
    Va a la página de KPIs, selecciona 'Ventas Nuevas Socios', genera y devuelve el DataFrame.
    """
    logging.info(f"Navegando a KPI_URL (Ventas Nuevos Socios): {KPI_URL}")
    print("➡ Entrando a KPIs (Ventas Nuevos Socios)...")
    page.goto(KPI_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    # Seleccionar tipo de reporte: Ventas Nuevas Socios
    seleccionar_tipo_reporte(page, "Ventas Nuevas Socios")

    # Clic en Generar
    click_boton_generar(page)

    # Esperar tabla con 'Sucursal'
    df_kpi = obtener_tabla_con_sucursal(page, "KPI Ventas Nuevos Socios")

    logging.info(
        f"KPI Ventas Nuevos Socios: tabla extraída con {len(df_kpi)} filas y {len(df_kpi.columns)} columnas."
    )
    print("✔ KPI Ventas Nuevos Socios descargado.")
    return df_kpi


# ============== Helper de reintentos por reporte ============== #

def ejecutar_con_reintentos(fn, nombre_reporte):
    """
    Ejecuta fn() con reintentos. Si falla, solo repite ese reporte.
    """
    ultimo_error = None
    for intento in range(1, MAX_RETRIES + 1):
        print(f"\n🔄 {nombre_reporte} - intento {intento}/{MAX_RETRIES}\n")
        logging.info(f"{nombre_reporte}: intento {intento}/{MAX_RETRIES}")
        try:
            return fn()
        except Exception as e:
            ultimo_error = e
            logging.warning(f"{nombre_reporte} fallo en intento {intento}: {e}")
            if intento < MAX_RETRIES:
                print(f"⚠ {nombre_reporte} falló, reintentando...")
                time.sleep(5)

    raise RuntimeError(
        f"{nombre_reporte}: falló después de {MAX_RETRIES} intentos. Último error: {ultimo_error}"
    )


# ================== main ================== #

def main():
    validar_config()
    logging.info("==== Inicio de ejecución: Dirección + KPI Desempeño + KPI Ventas NS ====")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not SHOW_BROWSER)
            context = browser.new_context()
            page = context.new_page()

            # 1) Login una sola vez
            hacer_login(page)

            # 2) Reporte Dirección
            df_dir = ejecutar_con_reintentos(
                lambda: descargar_reporte_direccion(page),
                "Reporte Dirección"
            )

            # 3) KPI Desempeño
            df_kpi = ejecutar_con_reintentos(
                lambda: descargar_kpi_desempeno(page),
                "KPI Desempeño"
            )

            # 4) KPI Ventas Nuevos Socios
            df_kpi_vns = ejecutar_con_reintentos(
                lambda: descargar_kpi_ventas_nuevos_socios(page),
                "KPI Ventas Nuevos Socios"
            )

    except Exception as e:
        msg = f"❌ Error general al obtener reportes: {e}"
        print(msg)
        logging.error(msg)
        send_whatsapp(msg)
        sys.exit(1)

    # ----- Guardar archivos si hubo éxito ----- #

    # Reporte Dirección
    filename_dir = f"ingresos_{timestamp}.xlsx"
    destino_dir = OUTPUT_DIR / filename_dir
    df_dir.to_excel(destino_dir, index=False)
    logging.info(f"Archivo Dirección guardado en: {destino_dir}")
    print(f"✅ Reporte Dirección guardado en: {destino_dir}")

    # KPI Desempeño
    filename_kpi = f"kpi_desempeno_{timestamp}.xlsx"
    destino_kpi = KPI_OUTPUT_DIR / filename_kpi
    df_kpi.to_excel(destino_kpi, index=False)
    logging.info(f"Archivo KPI Desempeño guardado en: {destino_kpi}")
    print(f"✅ KPI Desempeño guardado en: {destino_kpi}")

    # KPI Ventas Nuevos Socios
    filename_kpi_vns = f"kpi_ventas_nuevos_socios_{timestamp}.xlsx"
    destino_kpi_vns = KPI_VENTAS_NS_OUTPUT_DIR / filename_kpi_vns
    df_kpi_vns.to_excel(destino_kpi_vns, index=False)
    logging.info(f"Archivo KPI Ventas Nuevos Socios guardado en: {destino_kpi_vns}")
    print(f"✅ KPI Ventas Nuevos Socios guardado en: {destino_kpi_vns}")

    send_whatsapp(
        "✅ Reportes OK "
        f"({timestamp}). Dirección: {destino_dir.name}, "
        f"KPI Desempeño: {destino_kpi.name}, "
        f"KPI Ventas NS: {destino_kpi_vns.name}"
    )


if __name__ == "__main__":
    main()
