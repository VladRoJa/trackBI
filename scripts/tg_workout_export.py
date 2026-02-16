import os
import time
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

import pytz
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= TZ / Paths =================
TZ = pytz.timezone("America/Tijuana")

BASE_DIR = Path(__file__).resolve().parents[1]  # TRACK BI
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "tg_workout_export.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

# ================= ENV =================
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"

TRAINING_LOGIN_URL = os.getenv("TRAINING_LOGIN_URL", "https://app.tgmanager.com/auth")
TRAINING_REPORT_WORKOUT_URL = os.getenv("TRAINING_REPORT_WORKOUT_URL", "https://app.tgmanager.com/reports/workout")
TRAINING_CENTER_NAME = os.getenv("TRAINING_CENTER_NAME", "UltraGym & Fitness - Azahares")
TRAINING_USER = os.getenv("TRAINING_USER")
TRAINING_PASS = os.getenv("TRAINING_PASS")

# Perfil persistente (Edge) + outputs
TG_PROFILE_DIR = str((BASE_DIR / ".tg_profile").resolve())

OUT_DIR = (BASE_DIR / "data" / "rutinas").resolve()
RAW_DIR = (OUT_DIR / "raw").resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_TIMEOUT_MS = 240_000  # PBI tarda

# ================= Utils =================
def _now_date_tj() -> date:
    return datetime.now(TZ).date()

def _yesterday_tj() -> date:
    return _now_date_tj() - timedelta(days=1)

def _first_of_month(d: date) -> date:
    return d.replace(day=1)

def _fmt_ddmmyyyy(d: date) -> str:
    return d.strftime("%d/%m/%Y")

def _stealth(context):
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

def _maybe_click_by_text(page, texts, timeout_ms=600):
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(force=True)
                page.wait_for_timeout(250)
                return True
        except Exception:
            pass
    page.wait_for_timeout(timeout_ms)
    return False

def _first_visible(*locators):
    for loc in locators:
        try:
            if loc and loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None

def tg_wait_export_modal_anywhere(page, timeout_ms=60_000):
    """
    El modal de exportación a veces aparece en TOP (page) y a veces dentro del frame PBI.
    Lo buscamos en TODOS los frames.
    """
    titles = [
        "¿Qué datos quiere exportar?",
        "What data do you want to export?",
    ]

    deadline = time.time() + timeout_ms / 1000
    last_seen = None

    while time.time() < deadline:
        for fr in page.frames:
            for t in titles:
                try:
                    title_loc = fr.get_by_text(t, exact=False)
                    if title_loc.count() and title_loc.first.is_visible():
                        last_seen = (fr, t)
                        # intenta agarrar el dialog root
                        dialog = fr.locator("div[role='dialog']").filter(has=title_loc.first).first
                        if dialog.count():
                            return fr, dialog
                        return fr, None
                except Exception:
                    continue

        page.wait_for_timeout(250)

    raise PlaywrightTimeoutError(f"No apareció el modal de exportación. last_seen={last_seen}")


def tg_click_export_datos_from_pbi_menu(page, fr, timeout_ms=20_000):
    """
    Click robusto al item 'Exportar datos' dentro del menú de PowerBI (role=menu).
    """
    deadline = time.time() + timeout_ms / 1000
    last = None

    while time.time() < deadline:
        try:
            # menú PowerBI suele ser role=menu
            menu = fr.locator("[role='menu']").last
            if menu.count() and menu.is_visible():
                item = menu.locator("[role='menuitem'], [role='menuitemcheckbox'], [role='presentation']").filter(
                    has=fr.get_by_text("Exportar datos", exact=False)
                ).first

                if item.count() and item.is_visible():
                    item.click(force=True)
                    page.wait_for_timeout(250)
                    return True

                # fallback: click directo por texto, pero dentro del menú
                txt = menu.get_by_text("Exportar datos", exact=False).first
                if txt.count() and txt.is_visible():
                    txt.click(force=True)
                    page.wait_for_timeout(250)
                    return True
        except Exception as e:
            last = e

        page.wait_for_timeout(200)

    raise PlaywrightTimeoutError(f"No pude clickear 'Exportar datos' desde el menú PBI. last={last}")


# ================= Modals / blockers =================
def tg_close_annoying_modals(page):
    # cookies
    _maybe_click_by_text(page, ["Aceptar", "Accept"], timeout_ms=200)

    # NPS / overlays con X
    for _ in range(5):
        closed = False

        # botones close
        try:
            loc = page.locator(
                "button[aria-label*='Cerrar' i], button[title*='Cerrar' i], "
                "button[aria-label*='Close' i], button[title*='Close' i]"
            )
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(force=True)
                page.wait_for_timeout(250)
                closed = True
        except Exception:
            pass

        # texto X
        try:
            loc2 = page.get_by_text("×", exact=True)
            if loc2.count() > 0 and loc2.first.is_visible():
                loc2.first.click(force=True)
                page.wait_for_timeout(250)
                closed = True
        except Exception:
            pass

        if not closed:
            break

# ================= Frames =================
def tg_get_pbi_frame(page, timeout_ms=180_000):
    """
    Encuentra el iframe donde vive PowerBI: reportEmbed / app.powerbi.com
    """
    deadline = time.time() + timeout_ms / 1000
    last = []

    while time.time() < deadline:
        last = []
        for fr in page.frames:
            u = (fr.url or "").lower()
            last.append(u[:140])
            if "app.powerbi.com" in u and "reportembed" in u:
                return fr
        page.wait_for_timeout(500)

    print("❌ No encontré el frame de PowerBI. Frames vistos:")
    for u in last[:15]:
        print("  -", u)
    raise RuntimeError("No se encontró iframe reportEmbed (PowerBI).")

def tg_wait_for_slicers(fr, timeout_ms=180_000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if fr.locator("input.date-slicer-input").count() >= 2:
                return True
        except Exception:
            pass
        time.sleep(0.35)
    raise PlaywrightTimeoutError("Timeout esperando date-slicer-input (>=2) en frame PBI.")

# ================= TG Login =================
def tg_login_auto(page):
    if not TRAINING_USER or not TRAINING_PASS:
        raise RuntimeError("Faltan TRAINING_USER / TRAINING_PASS en .env")

    print("➡ [TG] Abriendo login...")
    page.goto(TRAINING_LOGIN_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(800)

    tg_close_annoying_modals(page)

    user_in = page.locator(
        "input[placeholder*='Usuario' i], input[aria-label*='Usuario' i], input[type='email'], input[type='text']"
    ).first
    pass_in = page.locator(
        "input[placeholder*='Contraseña' i], input[aria-label*='Contraseña' i], input[type='password']"
    ).first

    user_in.wait_for(state="visible", timeout=30_000)
    pass_in.wait_for(state="visible", timeout=30_000)

    user_in.click(force=True)
    user_in.fill("")
    user_in.type(TRAINING_USER, delay=40)

    pass_in.click(force=True)
    pass_in.fill("")
    pass_in.type(TRAINING_PASS, delay=40)

    pass_in.press("Tab")
    page.wait_for_timeout(800)

    btn = page.get_by_role("button", name="ACCEDER")
    btn.wait_for(state="visible", timeout=20_000)

    for _ in range(80):
        if btn.is_enabled():
            break
        page.wait_for_timeout(250)

    if not btn.is_enabled():
        raise RuntimeError("❌ [TG] ACCEDER sigue deshabilitado.")

    print("➡ [TG] Click ACCEDER (login)...")
    btn.click()
    page.wait_for_load_state("domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1200)
    tg_close_annoying_modals(page)

    # Selección de centro (si aparece)
    try:
        print("➡ [TG] Seleccionando centro...")
        select_box = page.locator("div[role='combobox'], .ant-select, nz-select, div.ant-select-selector").first
        if select_box.count() > 0:
            select_box.wait_for(state="visible", timeout=20_000)
            select_box.click(force=True)
            page.wait_for_timeout(500)

            opt = page.get_by_text(TRAINING_CENTER_NAME, exact=False).first
            opt.wait_for(state="visible", timeout=20_000)
            opt.click(force=True)
            page.wait_for_timeout(500)

            btn2 = page.get_by_role("button", name="ACCEDER")
            btn2.wait_for(state="visible", timeout=20_000)

            for _ in range(80):
                if btn2.is_enabled():
                    break
                page.wait_for_timeout(250)

            if not btn2.is_enabled():
                raise RuntimeError("❌ [TG] Segundo ACCEDER (centro) sigue deshabilitado.")

            print("➡ [TG] Click ACCEDER (centro)...")
            btn2.click()
            page.wait_for_load_state("domcontentloaded", timeout=90_000)
            page.wait_for_timeout(1200)

    except Exception as e:
        print(f"ℹ️ [TG] Centro no requerido / no pude seleccionarlo: {e}")

    print("✔ [TG] Login OK")

# ================= TG Report =================
def tg_goto_workout(page):
    print("➡ [TG] Yendo a /reports/workout ...")
    page.goto(TRAINING_REPORT_WORKOUT_URL, timeout=90_000)
    page.wait_for_load_state("domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1200)
    tg_close_annoying_modals(page)
    print(f"✔ [TG] URL actual: {page.url}")

# ================= Dates (JS en frame PBI) =================
def tg_set_dates_inputs_js(page, start_date: date, end_date: date):
    start_str = _fmt_ddmmyyyy(start_date)
    end_str = _fmt_ddmmyyyy(end_date)
    print(f"📅 [TG-JS] Set fechas: IZQ={start_str}  DER={end_str}")

    tg_close_annoying_modals(page)

    fr = tg_get_pbi_frame(page)
    tg_wait_for_slicers(fr, timeout_ms=180_000)

    cnt = fr.locator("input.date-slicer-input").count()
    print(f"   [TG-JS] frame slicers count={cnt}  frame_url={(fr.url or '')[:120]}")

    result = fr.evaluate(
        """
        ({startStr, endStr}) => {
          const inputs = Array.from(document.querySelectorAll("input.date-slicer-input"));
          if (inputs.length < 2) return {ok:false, reason:"inputs<2", count: inputs.length};

          const left = inputs[0];
          const right = inputs[1];

          function setInput(el, val){
            el.focus();
            el.value = val;
            el.dispatchEvent(new Event("input", {bubbles:true}));
            el.dispatchEvent(new Event("change", {bubbles:true}));
            el.dispatchEvent(new Event("blur", {bubbles:true}));
          }

          // derecha primero (FIN), luego izquierda (INICIO)
          setInput(right, endStr);
          setInput(left, startStr);

          return {ok:true, left:left.value, right:right.value};
        }
        """,
        {"startStr": start_str, "endStr": end_str},
    )

    print("   [TG-JS] result:", result)
    if not result.get("ok"):
        raise RuntimeError(f"[TG-JS] No pude setear fechas: {result}")

    page.wait_for_timeout(600)

# ================= Refresh: esperar tabla =================
TABLE_HEADER_HINTS = [
    "NombreApellidos",
    "Email",
    "Técnico",
    "N°Rutinas",
    "N°Pesajes",
    "Total Rutinas",
]

def tg_wait_refresh_until_table(page, timeout_ms=180_000):
    print("⏳ [TG] Esperando a que la tabla se refresque...")
    fr = tg_get_pbi_frame(page)

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            for t in TABLE_HEADER_HINTS:
                loc = fr.get_by_text(t, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    print("✅ [TG] Tabla lista.")
                    return True
        except Exception:
            pass
        page.wait_for_timeout(500)

    raise PlaywrightTimeoutError("Timeout esperando que la tabla aparezca (headers).")

# ================= Scroll correcto (contenido, no menú) =================
def tg_get_content_container(page):
    candidates = [
        "div.ant-layout-content",
        "div.ant-layout",
        "main",
        "body",
    ]
    for css in candidates:
        try:
            loc = page.locator(css).first
            if loc.count() and loc.is_visible():
                box = loc.bounding_box()
                if box and box["height"] > 300:
                    return loc
        except Exception:
            pass
    return page.locator("body")

def tg_scroll_to_table(page, timeout_ms=120_000):
    print("🧭 [TG] Scroll al área de tabla (contenido, no menú)...")
    tg_close_annoying_modals(page)

    fr = tg_get_pbi_frame(page)
    container = tg_get_content_container(page)

    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        # ¿ya vemos algún header de tabla?
        try:
            for t in TABLE_HEADER_HINTS:
                loc = fr.get_by_text(t, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    print("✅ [TG] Tabla detectada/visible.")
                    return True
        except Exception:
            pass

        # scroll sobre CONTENIDO
        try:
            container.hover()
        except Exception:
            pass

        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(600)

    raise PlaywrightTimeoutError("[TG] No logré llegar a la tabla (timeout).")

def tg_find_frame_with_pbi(page, timeout_ms=180_000):
    """
    Devuelve el frame donde vive el PowerBI embebido.
    Señales: input.date-slicer-input o reportEmbed/powerbi en URL.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for fr in page.frames:
            try:
                u = (fr.url or "").lower()
                if "powerbi" in u or "reportembed" in u:
                    return fr
                if fr.locator("input.date-slicer-input").count() >= 1:
                    return fr
            except Exception:
                pass
        page.wait_for_timeout(400)

    # fallback
    return page.main_frame


# ================= Export (PowerBI) =================
def tg_export_table(page, end_date: date):
    tg_close_annoying_modals(page)

    fr = tg_find_frame_with_pbi(page, timeout_ms=180_000)

    def _find_export_modal_anywhere(timeout_ms=20_000):
        """
        Devuelve (modal_frame, modal_root, export_btn) si el modal ya está abierto.
        modal_root puede ser None si solo se encuentra el botón.
        """
        deadline = time.time() + timeout_ms / 1000
        last = None

        while time.time() < deadline:
            for f in page.frames:
                try:
                    # botón Exportar del modal (es el más confiable)
                    btn = f.locator("button[data-testid='export-btn'], button[aria-label='Exportar'], button:has-text('Exportar')").first
                    if btn.count():
                        # OJO: no uses is_visible/is_enabled aquí (te puede timeoutear)
                        try:
                            btn.wait_for(state="attached", timeout=1000)
                            # modal root (dialog) si existe
                            dlg = btn.locator("xpath=ancestor::div[@role='dialog'][1]").first
                            if dlg.count():
                                return f, dlg, btn
                            return f, None, btn
                        except Exception as e:
                            last = e

                    # fallback por título del modal (a veces tarda el botón)
                    title = f.get_by_text("¿Qué datos quiere exportar?", exact=False).first
                    if title.count():
                        try:
                            title.wait_for(state="attached", timeout=1000)
                            dlg = f.locator("div[role='dialog']").filter(has=title).first
                            if dlg.count():
                                # busca botón dentro de ese dialog
                                btn2 = dlg.locator("button[data-testid='export-btn'], button[aria-label='Exportar'], button:has-text('Exportar')").first
                                if btn2.count():
                                    return f, dlg, btn2
                                return f, dlg, None
                            return f, None, None
                        except Exception as e:
                            last = e

                except Exception as e:
                    last = e
                    continue

            page.wait_for_timeout(200)

        return None, None, None  # no está abierto

    def _ensure_design_actual_selected(modal_frame, modal_root):
        """
        Fuerza que la opción seleccionada sea 'Datos con diseño actual' (NO resumidos).
        Lo hace leyendo el estado del radio y clickeando el radio correcto.
        """
        root = modal_root if (modal_root is not None) else modal_frame

        def _selected_label():
            # intenta inferir la tarjeta seleccionada buscando un radio marcado cerca del texto
            # (en PBI normalmente hay un input/radio con aria-checked=true o checked)
            try:
                # primero: radio/elemento marcado dentro de la tarjeta que contiene el texto
                design = root.locator(
                    "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[1]"
                ).first
                resum = root.locator(
                    "xpath=//*[contains(normalize-space(.),'Datos resumidos')]/ancestor::*[1]"
                ).first

                # Busca radios marcados dentro de cada tarjeta
                # (varía: role=radio, input[type=radio], mat-radio, etc.)
                def has_checked(card):
                    if card.count() == 0:
                        return False
                    return card.locator(
                        "[role='radio'][aria-checked='true'], input[type='radio']:checked, .mat-radio-checked"
                    ).count() > 0

                if has_checked(design):
                    return "design"
                if has_checked(resum):
                    return "resumen"
            except Exception:
                pass

            # fallback: mira si el “puntito” verde está dentro de la tarjeta (a veces es un svg/circle)
            try:
                if root.locator("xpath=//*[contains(.,'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]//*[contains(@class,'selected') or @aria-selected='true']").count():
                    return "design"
            except Exception:
                pass

            return None

        # 1) Si ya está design, no hagas nada
        if _selected_label() == "design":
            return True

        # 2) Click MUY específico: el radio/círculo dentro de la tarjeta "Datos con diseño actual"
        # Intentamos varios selectores porque PBI cambia DOM según versión.
        radio_candidates = [
            # role radio dentro de la tarjeta
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]//*[@role='radio']",
            # input radio dentro de la tarjeta
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]//input[@type='radio']",
            # círculo típico (a veces es un span antes del texto)
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]//span[contains(@class,'radio') or contains(@class,'mat-radio')]",
            # fallback: click al texto pero usando doble click para evitar “toggle” raro
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]",
        ]

        clicked = False
        for sel in radio_candidates:
            try:
                loc = root.locator(sel).first
                if loc.count():
                    loc.click(force=True, timeout=3000)
                    page.wait_for_timeout(200)
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            return False

        # 3) Confirma que quedó seleccionado. Si no, reintenta una vez más.
        page.wait_for_timeout(300)
        if _selected_label() == "design":
            return True

        # segundo intento: click al texto/tarjeta completa
        try:
            root.get_by_text("Datos con diseño actual", exact=False).first.click(force=True, timeout=3000)
            page.wait_for_timeout(300)
        except Exception:
            pass

        return _selected_label() == "design"

    def _btn_disabled_fast(btn) -> bool:
        """
        Chequeo rápido sin is_enabled() (que te da timeout).
        """
        try:
            return btn.evaluate("""
                (b) => {
                  const d = b.getAttribute('disabled');
                  const ad = b.getAttribute('aria-disabled');
                  const cls = (b.className || '').toString();
                  return !!d || ad === 'true' || cls.includes('disabled');
                }
            """)
        except Exception:
            return False

    print("🧭 [TG] Asegurando tabla visible...")
    anchors = ["NombreApellidos", "Email", "Técnico", "N°Rutinas", "N°Pesajes", "Valoración"]
    anchor = None
    for t in anchors:
        loc = fr.get_by_text(t, exact=False)
        if loc.count():
            anchor = loc.first
            break
    if not anchor:
        raise RuntimeError("[TG] No encontré headers de tabla dentro del frame PBI.")

    anchor.scroll_into_view_if_needed()
    page.wait_for_timeout(400)

    print("➡ [TG] Localizando contenedor del visual (PowerBI)...")
    visual = anchor.locator("xpath=ancestor-or-self::*[@data-visual-id][1]")
    if visual.count() == 0:
        visual = anchor.locator("xpath=ancestor-or-self::div[contains(@class,'visualContainer')][1]")
    if visual.count() == 0:
        raise RuntimeError("[TG] No pude resolver el contenedor del visual para encontrar los 3 puntos.")

    # 1) Si el modal YA está abierto, no intentes reabrir menú
    modal_frame, modal_root, export_btn = _find_export_modal_anywhere(timeout_ms=1500)
    if modal_frame:
        print("✅ [TG] Modal ya estaba visible; salto abrir menú.")
    else:
        # Activar toolbar del visual
        try:
            visual.hover(timeout=5000)
        except Exception:
            visual.click(timeout=5000, force=True)
        page.wait_for_timeout(250)

        print("➡ [TG] Buscando 'Más opciones' DENTRO del visual...")
        more = visual.locator(
            "button[aria-label*='Más opciones' i], button[title*='Más opciones' i], "
            "button[aria-label*='More options' i], button[title*='More options' i]"
        ).first

        if more.count() == 0:
            more = fr.locator(
                "button[aria-label*='Más opciones' i], button[title*='Más opciones' i], "
                "button[aria-label*='More options' i], button[title*='More options' i]"
            ).first

        more.wait_for(state="visible", timeout=30_000)

        # 2) Abrir menú y click Exportar datos con reintentos
        for attempt in range(1, 6):
            print(f"➡ [TG] Intento {attempt}/5 abrir menú + Exportar datos...")

            try:
                visual.hover(timeout=2000)
            except Exception:
                try:
                    visual.click(timeout=2000, force=True)
                except Exception:
                    pass

            page.wait_for_timeout(120)

            try:
                more.click(force=True, timeout=3000)
            except Exception:
                pass

            page.wait_for_timeout(220)

            # Click item "Exportar datos" (tu hallazgo: <span>Exportar datos</span>)
            export_menu_item = fr.locator("span:has-text('Exportar datos')").first
            if export_menu_item.count() == 0:
                export_menu_item = fr.get_by_text("Exportar datos", exact=False).first

            try:
                export_menu_item.wait_for(state="visible", timeout=4000)
                export_menu_item.click(force=True, timeout=4000)
            except Exception as e:
                print(f"⚠ [TG] No pude clickear Exportar datos: {e}")
                try:
                    visual.click(force=True, timeout=1500)
                except Exception:
                    pass
                page.wait_for_timeout(450)
                continue

            # Esperar modal real
            print("➡ [TG] Esperando modal exportación (export-btn/título) ...")
            modal_frame, modal_root, export_btn = _find_export_modal_anywhere(timeout_ms=20_000)
            if modal_frame:
                print("✅ [TG] Modal exportación detectado.")
                break

            print("⚠ [TG] No apareció modal aún; reintento…")
            page.wait_for_timeout(650)
        else:
            raise RuntimeError("No logré abrir el modal de exportación tras 5 intentos.")

    # 3) Con modal detectado: garantizar root/btn
    if export_btn is None:
        # si encontramos dialog pero no botón, vuelve a buscar botón dentro del dialog
        if modal_root is not None:
            export_btn = modal_root.locator(
                "button[data-testid='export-btn'], button.exportButton, button[aria-label='Exportar'], button:has-text('Exportar')"
            ).first
        else:
            export_btn = modal_frame.locator(
                "button[data-testid='export-btn'], button.exportButton, button[aria-label='Exportar'], button:has-text('Exportar')"
            ).first

    if export_btn is None or export_btn.count() == 0:
        raise RuntimeError("[TG] Detecté el modal pero no pude resolver el botón Exportar.")

    # Asegura que el botón exista en DOM
    export_btn.wait_for(state="attached", timeout=60_000)

    # 4) Forzar “Datos con diseño actual”
    try:
        _ensure_design_actual_selected(modal_frame, modal_root)
    except Exception:
        pass

    # 5) Esperar a que el botón NO esté disabled (sin is_enabled)
    # (a veces tarda por render/material)
    end_wait = time.time() + 25
    while time.time() < end_wait:
        if not _btn_disabled_fast(export_btn):
            break
        page.wait_for_timeout(250)

    # 6) Click Exportar + esperar descarga (reintentos)
    print("⬇️ [TG] Click Exportar + esperando descarga...")
    download = None
    last_err = None

    for attempt in range(1, 6):
        try:
            print(f"➡ [TG] Click Exportar intento {attempt}/5...")

            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dlinfo:
                # click normal
                try:
                    export_btn.click(timeout=3000, force=True)
                except Exception:
                    pass
                # JS click extra (material/iframe)
                try:
                    export_btn.evaluate("b => b.click()")
                except Exception:
                    pass

            download = dlinfo.value
            print("✅ [TG] Download capturado.")
            break

        except Exception as e:
            last_err = e
            print(f"⚠ [TG] No cayó download en intento {attempt}: {e}")
            page.wait_for_timeout(1200)

            # si el modal se cerró, esperamos un poco por si el download tarda
            try:
                if modal_root is not None:
                    # no uses is_visible (puede timeoutear); solo espera un poco
                    pass
            except Exception:
                pass

            # re-seleccionar diseño actual por si cambió a resumidos
            try:
                _ensure_design_actual_selected(modal_frame, modal_root)
            except Exception:
                pass

    if download is None:
        raise RuntimeError(f"No logré capturar la descarga tras reintentos. last_err={last_err}")

    suggested = download.suggested_filename or "data.xlsx"
    hora = datetime.now(TZ).strftime("%H-%M")
    raw_path = RAW_DIR / f"tg_workout_{end_date:%Y-%m-%d}_{hora}_{suggested}"
    download.save_as(str(raw_path))

    print(f"✅ [TG] Descarga OK: {raw_path.name}")
    logging.info(f"[TG] Descarga guardada: {raw_path}")

    latest_path = OUT_DIR / "tg_workout.xlsx"
    try:
        if latest_path.exists():
            latest_path.unlink()
        raw_path.replace(latest_path)
        print(f"✅ [TG] Latest: {latest_path.name}")
    except Exception as e:
        logging.warning(f"[TG] No pude mover a latest: {e}")

    return True

# ================= MAIN =================
def main():
    if not TRAINING_USER or not TRAINING_PASS:
        print("❌ Falta TRAINING_USER/TRAINING_PASS en .env")
        return

    end_d = _yesterday_tj()
    start_d = _first_of_month(end_d)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=TG_PROFILE_DIR,
            channel="msedge",
            headless=not SHOW_BROWSER,
            accept_downloads=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=msEdgeTranslate",
            ],
        )
        _stealth(context)

        page = context.new_page()

        page.on("console", lambda m: print(f"🧾 [console] {m.type}: {m.text}"))
        page.on("crash", lambda: print("💥 [TG] La página crasheó"))
        page.on("close", lambda: print("🔥 [TG] La página se cerró sola"))

        tg_goto_workout(page)

        if "/auth" in page.url:
            tg_login_auto(page)
            tg_goto_workout(page)

        # fechas (JS dentro del frame)
        tg_set_dates_inputs_js(page, start_d, end_d)

        # esperar tabla
        tg_wait_refresh_until_table(page, timeout_ms=180_000)

        # exportar
        tg_export_table(page, end_d)


        context.close()

if __name__ == "__main__":
    main()
