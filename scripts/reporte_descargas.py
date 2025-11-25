import os
import sys
import logging
import time
from datetime import datetime
from pathlib import Path
from io import StringIO
import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]  # carpeta TRACK BI
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "reporte_descargas.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

USER = os.getenv("DIRECCION_USER")
PASS = os.getenv("DIRECCION_PASS")
LOGIN_URL = os.getenv("DIRECCION_LOGIN_URL")
REPORTES_URL = os.getenv("REPORTES_URL")

# Carpeta destino para los archivos descargados (SIN fecha)
OUTPUT_DIR = Path(
    os.getenv(
        "DESCARGAS_OUTPUT_DIR",
        BASE_DIR / "data/descargas"
    )
).resolve()

# Mostrar navegador (1 = visible, 0 = headless)
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"

MAX_RETRIES = 3


# ============================================================
# VALIDACIÓN CONFIG
# ============================================================

def validar_config():
    faltan = []
    if not USER:
        faltan.append("DIRECCION_USER")
    if not PASS:
        faltan.append("DIRECCION_PASS")
    if not LOGIN_URL:
        faltan.append("DIRECCION_LOGIN_URL")

    if faltan:
        msg = f"Faltan variables en .env: {', '.join(faltan)}"
        logging.error(msg)
        print(msg)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.info(f"OUTPUT_DIR={OUTPUT_DIR}")


# ============================================================
# HELPERS PLAYWRIGHT
# ============================================================

def seleccionar_fecha(page, input_selector: str, dia: int | None, usar_hoy: bool = False):
    """
    Abre el datepicker asociado al input (ej. '#FechaInicio', '#FechaFin')
    y selecciona un día.
    """
    print(f"➡ Abriendo datepicker en {input_selector}...")

    # 1) Click directo al input
    page.click(input_selector)
    time.sleep(0.3)

    # 2) Abrir datepicker usando el ícono adyacente
    icon_selector = f"{input_selector} + span"
    try:
        page.click(icon_selector)
    except:
        pass  # si no hay ícono, igual ya se abrió

    time.sleep(0.5)

    # 3) Seleccionar día
    if usar_hoy:
        locator = page.locator("td.day.today")
        locator.first.click()
        return

    locator = page.locator(f"td.day", has_text=str(dia))
    locator.first.click()
    time.sleep(0.4)


def rellenar_fechas_corte_caja(page):
    """
    Fecha Inicio = día 1 del mes actual
    Fecha Fin    = día de hoy

    En lugar de usar el datepicker, tomamos los dos primeros
    <input type="text"> del formulario y escribimos las fechas
    simulando tipeo.
    """
    hoy = datetime.now()
    inicio_mes = hoy.replace(day=1)

    fecha_inicio_str = inicio_mes.strftime("%m/%d/%Y")  # ej. 11/01/2025
    fecha_fin_str    = hoy.strftime("%m/%d/%Y")         # ej. 11/23/2025

    print(f"➡ Fecha Inicio = {fecha_inicio_str}")
    print(f"➡ Fecha Fin    = {fecha_fin_str}")

    # Tomamos todos los inputs de texto de la página
    inputs = page.locator("input[type='text']")
    total = inputs.count()
    print(f"   inputs[type='text'] encontrados: {total}")

    if total < 2:
        raise RuntimeError(
            f"Esperaba al menos 2 inputs de texto para fechas, pero encontré {total}"
        )

    # Asumimos:
    #   0 -> Fecha Inicio
    #   1 -> Fecha Fin
    campo_inicio = inputs.nth(0)
    campo_fin    = inputs.nth(1)

    # Rellenar simulando escritura para que respete mascarillas de fecha
    for campo, valor, nombre in [
        (campo_inicio, fecha_inicio_str, "Fecha Inicio"),
        (campo_fin,    fecha_fin_str,    "Fecha Fin"),
    ]:
        print(f"   escribiendo {nombre} = {valor}")
        campo.click()
        # limpiar por si trae algo
        campo.fill("")
        # type simula tecleo (dispara eventos de input/change del front)
        campo.type(valor, delay=50)
        time.sleep(0.3)

def click_tab_membresia(page):
    """
    Cambia al tab 'Membresia/Membresía'.
    No hacemos waits raros, solo intentamos varios tipos de clic.
    """
    print("➡ Cambiando a tab 'Membresía'...")

    textos_posibles = ["Membresía", "Membresia"]  # con y sin tilde

    for texto in textos_posibles:
        # 1) Botón por rol
        try:
            page.get_by_role("button", name=texto, exact=False).click()
            print(f"✔ Tab '{texto}' (get_by_role)")
            return
        except:
            pass

        # 2) Enlace <a>
        try:
            page.locator(f"a:has-text('{texto}')").first.click()
            print(f"✔ Tab '{texto}' (locator <a>)")
            return
        except:
            pass

        # 3) Cualquier elemento con ese texto
        try:
            page.get_by_text(texto, exact=False).first.click()
            print(f"✔ Tab '{texto}' (get_by_text)")
            return
        except:
            pass

    print("❌ No se pudo cambiar al tab 'Membresía'")
    raise RuntimeError("No se encontró un tab clickeable 'Membresía/Membresia'.")


def hacer_login(page):
    """
    Login una sola vez (igual que el otro script).
    """
    logging.info("Iniciando login en Gasca (reportes)...")
    print("➡ Yendo a pantalla de login...")
    page.goto(LOGIN_URL, timeout=60_000)

    print("➡ Llenando usuario y contraseña...")
    page.get_by_label("Usuario").fill(USER)
    page.get_by_label("Contraseña").fill(PASS)
    print("➡ Clic en INICIAR SESIÓN...")
    page.get_by_role("button", name="INICIAR SESIÓN").click()
    page.wait_for_load_state("networkidle")
    print("✔ Login completado.")

    # Manejar posible 404 con "Ir a Inicio"
    try:
        ir_a_inicio = page.get_by_text("Ir a Inicio")
        if ir_a_inicio.count() > 0:
            logging.info("Detectado 404 tras login. Clic en 'Ir a Inicio'.")
            print("⚠ Salió 404, clic en 'Ir a Inicio'...")
            ir_a_inicio.first.click()
            page.wait_for_load_state("networkidle")
    except Exception:
        pass


def seleccionar_tipo_reporte(page, texto_opcion: str):
    """
    Selecciona una opción del combo 'Tipo de Reporte' usando JS directo.
    Esto funciona para 'Reporte Corte De Caja', 'Reporte Cargos Recurrentes', etc.
    """
    logging.info(f"Seleccionando tipo de reporte '{texto_opcion}'...")
    print(f"➡ Seleccionando tipo de reporte '{texto_opcion}'...")

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

                // Pantalla de Reportes: el primer <select> es "Tipo de Reporte"
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
            logging.info(f"Tipo de reporte '{texto_opcion}' seleccionado correctamente.")
            time.sleep(1)  # pequeña pausa para que el frontend procese
            return

        time.sleep(1)

    raise RuntimeError(
        f"No se pudo seleccionar tipo de reporte '{texto_opcion}' "
        f"después de {timeout_s}s. Último resultado: {ultimo_result}"
    )


def click_boton_generar(page):
    """
    Clic robusto en el botón azul 'Generar'.
    """
    logging.info("Buscando botón 'Generar'...")
    print("➡ Buscando botón 'Generar'...")

    # Intento 1: get_by_role
    try:
        page.get_by_role("button", name="Generar").click()
        print("✔ Click en 'Generar' (get_by_role).")
        return
    except Exception as e:
        logging.warning(f"No se pudo cliclear 'Generar' por get_by_role: {e}")

    # Intento 2: locator por texto
    try:
        page.locator("button:has-text('Generar')").first.click()
        print("✔ Click en 'Generar' (button:has-text).")
        return
    except Exception as e:
        logging.warning(f"No se pudo cliclear 'Generar' por button:has-text: {e}")

    # Intento 3: cualquier elemento con texto "Generar"
    try:
        page.get_by_text("Generar", exact=False).first.click()
        print("✔ Click en 'Generar' (get_by_text).")
        return
    except Exception as e:
        logging.error(f"No se pudo cliclear 'Generar': {e}")
        raise RuntimeError("No se pudo hacer clic en el botón 'Generar'.")


def click_tab(page, nombre_tab: str):
    """
    Hace clic en pestañas tipo 'Producto', 'Membresia', 'Todo'.
    """
    logging.info(f"Haciendo clic en tab '{nombre_tab}'...")
    print(f"➡ Cambiando a tab '{nombre_tab}'...")

    # botón por rol
    try:
        page.get_by_role("button", name=nombre_tab).click()
        print(f"✔ Tab '{nombre_tab}' (get_by_role).")
        return
    except Exception:
        pass

    # link o botón por texto
    try:
        page.get_by_text(nombre_tab, exact=False).first.click()
        print(f"✔ Tab '{nombre_tab}' (get_by_text).")
        return
    except Exception as e:
        logging.error(f"No se pudo cambiar a tab '{nombre_tab}': {e}")
        raise RuntimeError(f"No se pudo cambiar a tab '{nombre_tab}'.")


def descargar_excel_desde_tabla(
    page,
    nombre_reporte: str,
    nombre_archivo: str,
    usar_tab: str | None = None
):
    """
    Desde un reporte ya generado:
      - (opcional) hace clic en una pestaña (ej. 'Membresia')
      - clic en Exportar
      - clic en Excel
      - espera el download y lo guarda en OUTPUT_DIR/nombre_archivo
    """
    logging.info(f"Preparando descarga Excel para {nombre_reporte}...")
    print(f"➡ Preparando descarga Excel para {nombre_reporte}...")

    if usar_tab:
        # 👉 Si es Membresía, usamos el helper especial
        if usar_tab.lower().startswith("membres"):
            click_tab_membresia(page)
        else:
            click_tab(page, usar_tab)
        time.sleep(2)  # que cambie la tabla


    # Botón "Exportar"
    export_btn = None
    try:
        export_btn = page.get_by_role("button", name="Exportar")
    except Exception:
        pass

    if not export_btn:
        try:
            export_btn = page.locator("button:has-text('Exportar')").first
        except Exception as e:
            logging.error(f"No se encontró botón 'Exportar' para {nombre_reporte}: {e}")
            raise RuntimeError("No se encontró botón 'Exportar'.")

    export_btn.scroll_into_view_if_needed()
    export_btn.click()
    time.sleep(1)  # abrir menú

    # Click en "Excel" con expect_download
    print("➡ Clic en 'Excel' (esperando descarga)...")
    try:
        with page.expect_download(timeout=60_000) as dl_info:  # 60s máx
            try:
                page.get_by_text("Excel", exact=False).first.click()
            except Exception:
                page.locator("text=Excel").first.click()
        download = dl_info.value
    except PlaywrightTimeoutError as e:
        logging.error(f"{nombre_reporte}: timeout esperando download de Excel: {e}")
        raise RuntimeError(f"{nombre_reporte}: no se pudo iniciar/terminar la descarga de Excel en 60s")

    destino = OUTPUT_DIR / nombre_archivo
    if destino.exists():
        destino.unlink()

    download.save_as(str(destino))
    logging.info(f"{nombre_reporte}: archivo guardado en {destino}")
    print(f"✅ {nombre_reporte} guardado en: {destino}")
    return destino

def esperar_tabla_con_registros(page, min_filas: int = 10, timeout: int = 120):
    """
    Espera hasta que exista una tabla con al menos `min_filas` filas en <tbody>.
    Se usa para asegurarnos de que Venta Total ya cargó antes de exportar.
    """
    print(f"⏳ Esperando a que la tabla tenga al menos {min_filas} filas...")

    start = time.time()
    ultimo_conteo = 0

    while time.time() - start < timeout:
        try:
            filas = page.locator("table tbody tr")
            count = filas.count()
            ultimo_conteo = count
            print(f"   Filas actuales en tabla: {count}")

            if count >= min_filas:
                print("✔ Tabla lista para exportar.")
                return
        except Exception:
            # si algo falla en el locator, ignoramos y volvemos a intentar
            pass

        time.sleep(1)

    raise RuntimeError(
        f"La tabla no alcanzó {min_filas} filas en {timeout} segundos "
        f"(último conteo={ultimo_conteo})."
    )

def extraer_tabla_principal_venta_total(page):
    """
    Recorre todas las tablas de la página de Venta Total y devuelve
    la más grande (filas * columnas) como DataFrame.
    La usamos en lugar de Exportar -> Excel.
    """
    tablas = page.locator("table")
    total = tablas.count()

    if total == 0:
        raise RuntimeError("Venta Total: no se encontró ninguna tabla en la página.")

    logging.info(f"Venta Total: se encontraron {total} tablas. Buscando la más grande...")
    best_df = None
    best_score = 0

    for i in range(total):
        try:
            html_table = tablas.nth(i).evaluate("el => el.outerHTML")
            df_list = pd.read_html(StringIO(html_table))
        except Exception as e:
            logging.warning(f"Venta Total: error leyendo tabla {i}: {e}")
            continue

        for df in df_list:
            if df is None or df.empty:
                continue

            filas, columnas = df.shape
            score = filas * columnas
            logging.info(f"Venta Total: tabla {i} candidata {filas}x{columnas} (score={score})")

            if score > best_score:
                best_score = score
                best_df = df

    if best_df is None or best_df.empty:
        raise RuntimeError("Venta Total: no se pudo determinar una tabla principal (todas vacías).")

    logging.info(f"Venta Total: tabla seleccionada con shape={best_df.shape}")
    return best_df




# ============================================================
# REPORTES ESPECÍFICOS
# ============================================================

def descargar_reporte_corte_caja(page):
    """
    Flujo:
      - Ir a /Modulo/Reporte/Index
      - Tipo de Reporte = 'Reporte Corte De Caja'
      - Fecha Inicio = 1 del mes actual (calendar picker)
      - Fecha Fin    = hoy (calendar picker)
      - Horas en blanco, Sucursal y Empleado en 'Seleccione...'
      - Generar
      - Tab 'Membresia'
      - Exportar -> Excel
    """
    logging.info("==== Descarga: Reporte Corte De Caja ====")
    print("\n🔹 Descargando 'Reporte Corte De Caja'...\n")

    # Ir a la pantalla de reportes
    print("➡ Entrando a módulo de Reportes...")
    page.goto(REPORTES_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    # Seleccionar tipo de reporte
    seleccionar_tipo_reporte(page, "Reporte Corte De Caja")

    # 👉 Rellenar fechas usando el datepicker (clics reales)
    rellenar_fechas_corte_caja(page)

    # Horas las dejamos vacías, sucursal/empleado en "Seleccione..."
    # Generar reporte
    click_boton_generar(page)

    # Esta pantalla tarda: damos unos segundos y luego esperamos al botón Exportar
    print("⏳ Esperando a que el reporte termine de cargar...")
    time.sleep(5)

    # Esperar a que aparezca el botón Exportar (en la tabla)
    page.wait_for_selector("button:has-text('Exportar')", timeout=120_000)
    print("✔ Reporte Corte De Caja cargado.")

    # 👉 Cambiar explícitamente al tab 'Membresía'
    click_tab_membresia(page)

    # Descargar Excel (ya NO usamos usar_tab para evitar doble lógica)
    return descargar_excel_desde_tabla(
        page,
        nombre_reporte="Reporte Corte De Caja (Membresia)",
        nombre_archivo="corte_caja.xlsx",
        usar_tab=None,   # ← importante
    )

def descargar_reporte_venta_total(page):
    """
    Flujo:
      - Tipo de Reporte = 'Reporte Venta Total'
      - Fecha Inicio = 1 del mes actual
      - Fecha Fin    = hoy
      - Sucursal en 'Seleccione...'
      - Generar
      - Esperar a que la tabla tenga registros
      - Leer tabla HTML con pandas
      - Guardar a OUTPUT_DIR/venta_total.xlsx
    """
    logging.info("==== Descarga: Reporte Venta Total ====")
    print("\n🔹 Descargando 'Reporte Venta Total'...\n", flush=True)

    # Ir a la pantalla de reportes (por si venimos de otro reporte)
    print("➡ Entrando a módulo de Reportes...", flush=True)
    page.goto(REPORTES_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    # Seleccionar tipo de reporte
    seleccionar_tipo_reporte(page, "Reporte Venta Total")

    # Fechas: 1 del mes actual -> hoy
    hoy = datetime.now()
    inicio_mes = hoy.replace(day=1)

    fecha_inicio_str = inicio_mes.strftime("%m/%d/%Y")
    fecha_fin_str    = hoy.strftime("%m/%d/%Y")

    print(f"➡ Fecha Inicio = {fecha_inicio_str}", flush=True)
    print(f"➡ Fecha Fin    = {fecha_fin_str}", flush=True)

    # Inputs de texto (las fechas)
    inputs = page.locator("input[type='text']")
    total = inputs.count()
    print(f"   inputs[type='text'] encontrados: {total}", flush=True)

    if total < 2:
        raise RuntimeError(
            f"Venta Total: esperaba al menos 2 inputs de texto para fechas, pero encontré {total}"
        )

    campo_inicio = inputs.nth(0)
    campo_fin    = inputs.nth(1)

    for campo, valor, nombre in [
        (campo_inicio, fecha_inicio_str, "Fecha Inicio"),
        (campo_fin,    fecha_fin_str,    "Fecha Fin"),
    ]:
        print(f"   escribiendo {nombre} = {valor}", flush=True)
        campo.click()
        campo.fill("")
        campo.type(valor, delay=50)
        time.sleep(0.3)

    # Generar reporte
    click_boton_generar(page)

    # Esperar a que exista botón Exportar (indicador de que la tabla se creó)
    print("⏳ Esperando a que aparezca el botón 'Exportar'...", flush=True)
    page.wait_for_selector("button:has-text('Exportar')", timeout=120_000)

    # Luego esperar a que la tabla tenga X filas
    esperar_tabla_con_registros(page, min_filas=10, timeout=120)
    print("✔ Reporte Venta Total cargado con registros.", flush=True)

    # 👉 En lugar de Exportar -> Excel, leemos la tabla HTML y guardamos a xlsx
    print("➡ Extrayendo tabla principal de Venta Total desde HTML...", flush=True)
    df_venta = extraer_tabla_principal_venta_total(page)

    destino = OUTPUT_DIR / "venta_total.xlsx"
    if destino.exists():
        destino.unlink()

    df_venta.to_excel(destino, index=False)
    logging.info(f"Reporte Venta Total guardado en: {destino}")
    print(f"✅ Reporte Venta Total guardado en: {destino}", flush=True)

    return destino

def descargar_reporte_cargos_recurrentes(page):
    """
    Flujo:
      - Ir a /Modulo/Reporte/Index
      - Tipo de Reporte = 'Reporte Cargos Recurrentes'
      - Fecha Inicio / Fecha Fin (las primeras dos)
      - Generar
      - Exportar → Excel
    """

    logging.info("==== Descarga: Reporte Cargos Recurrentes ====")
    print("\n🔹 Descargando 'Reporte Cargos Recurrentes'...\n")

    # Ir a reportes
    print("➡ Entrando a módulo de Reportes...")
    page.goto(REPORTES_URL, timeout=120_000)
    page.wait_for_load_state("networkidle")

    # Seleccionar tipo repo
    seleccionar_tipo_reporte(page, "Reporte Cargos Recurrentes")

    # Rellenar fechas (las primeras dos cajas de texto)
    hoy = datetime.now()
    inicio_mes = hoy.replace(day=1)

    fecha_inicio_str = inicio_mes.strftime("%m/%d/%Y")
    fecha_fin_str = hoy.strftime("%m/%d/%Y")

    print(f"➡ Fecha Inicio = {fecha_inicio_str}")
    print(f"➡ Fecha Fin    = {fecha_fin_str}")

    # Inputs tipo texto
    inputs = page.locator("input[type='text']")
    total_inputs = inputs.count()
    print(f"   inputs[type='text'] encontrados: {total_inputs}")

    if total_inputs < 2:
        raise RuntimeError("No se encontraron los dos inputs principales de fecha.")

    campo_inicio = inputs.nth(0)
    campo_fin = inputs.nth(1)

    for campo, valor, nombre in [
        (campo_inicio, fecha_inicio_str, "Fecha Inicio"),
        (campo_fin, fecha_fin_str, "Fecha Fin")
    ]:
        print(f"   escribiendo {nombre} = {valor}")
        campo.click()
        campo.fill("")
        campo.type(valor, delay=50)
        time.sleep(0.3)

    # Generar
    click_boton_generar(page)

    print("⏳ Esperando a que el reporte termine de cargar...")
    time.sleep(5)

    # Esperar que existan varias filas en la tabla
    try:
        page.wait_for_selector("table tbody tr", timeout=20_000)
    except:
        print("⚠ La tabla no cargó registros visibles, seguimos con Exportar...")

    # Click Exportar → Excel
    print("➡ Preparando descarga Excel...")
    return descargar_excel_desde_tabla(
        page,
        nombre_reporte="Reporte Cargos Recurrentes",
        nombre_archivo="cargos_recurrentes.xlsx",
        usar_tab=None  # no hay tabs en este reporte
    )



# ============================================================
# REINTENTOS
# ============================================================

def ejecutar_con_reintentos(fn, nombre_reporte):
    ultimo_error = None
    for intento in range(1, MAX_RETRIES + 1):
        print(f"\n🔄 {nombre_reporte} - intento {intento}/{MAX_RETRIES}\n")
        logging.info(f"{nombre_reporte}: intento {intento}/{MAX_RETRIES}")
        try:
            return fn()
        except Exception as e:
            ultimo_error = e
            logging.warning(f"{nombre_reporte} falló en intento {intento}: {e}")
            if intento < MAX_RETRIES:
                print(f"⚠ {nombre_reporte} falló, reintentando en 5 segundos...")
                time.sleep(5)

    raise RuntimeError(
        f"{nombre_reporte}: falló después de {MAX_RETRIES} intentos. "
        f"Último error: {ultimo_error}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    validar_config()
    logging.info("==== Inicio de ejecución reporte_descargas.py ====")

    inicio_total = time.time()
    print("🚀 Iniciando reporte_descargas.py", flush=True)

    try:
        with sync_playwright() as p:
            # en Actions siempre va headless porque SHOW_BROWSER=0 en el .env
            browser = p.chromium.launch(headless=not SHOW_BROWSER)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            # 1) Login
            print("➡ [1/4] Haciendo login en Gasca...", flush=True)
            hacer_login(page)
            print("✅ Login OK", flush=True)

            # 2) Corte de caja
            print("➡ [2/4] Descargando REPORTE CORTE DE CAJA...", flush=True)
            ejecutar_con_reintentos(
                lambda: descargar_reporte_corte_caja(page),
                "Reporte Corte De Caja"
            )
            print("✅ Corte de caja descargado", flush=True)

            # 3) Venta total
            print("➡ [3/4] Descargando REPORTE VENTA TOTAL...", flush=True)
            ejecutar_con_reintentos(
                lambda: descargar_reporte_venta_total(page),
                "Reporte Venta Total"
            )
            print("✅ Venta total descargada", flush=True)

            # 4) Cargos recurrentes
            print("➡ [4/4] Descargando REPORTE CARGOS RECURRENTES...", flush=True)
            ejecutar_con_reintentos(
                lambda: descargar_reporte_cargos_recurrentes(page),
                "Reporte Cargos Recurrentes"
            )
            print("✅ Cargos recurrentes descargados", flush=True)

            browser.close()

    except Exception as e:
        msg = f"❌ Error general en reporte_descargas.py: {e}"
        print(msg, flush=True)
        logging.error(msg)
        sys.exit(1)

    dur = time.time() - inicio_total
    print(f"\n🎉 reporte_descargas.py terminado sin errores en {dur:.1f} s.\n", flush=True)


if __name__ == "__main__":
    main()
