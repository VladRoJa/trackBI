"""
tg_workout_export.py
--------------------
Exporta el reporte Workout (PowerBI embebido) desde TrainingGym (tgmanager),
forzando "Datos con diseño actual" y guardando el archivo en:
  data/rutinas/raw/  (nombre con fecha/hora)
  data/rutinas/tg_workout.xlsx (latest)

Diseñado para correr:
- Local (SHOW_BROWSER=1): usa perfil persistente (.tg_profile) en Edge.
- GitHub Actions (SHOW_BROWSER=0): usa browser/context normal (sin perfil) para menos flakiness.

Artifacts de debug siempre en: scripts/artifacts/
"""

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

ARTIFACTS_DIR = (BASE_DIR / "scripts" / "artifacts").resolve()
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOGS_DIR / "tg_workout_export.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

load_dotenv(ENV_PATH)

# ================= ENV =================
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "0") == "1"
DEBUG = os.getenv("DEBUG", "0") == "1"

TRAINING_LOGIN_URL = os.getenv("TRAINING_LOGIN_URL", "https://app.tgmanager.com/auth")
TRAINING_REPORT_WORKOUT_URL = os.getenv(
    "TRAINING_REPORT_WORKOUT_URL", "https://app.tgmanager.com/reports/workout"
)
TRAINING_CENTER_NAME = os.getenv("TRAINING_CENTER_NAME", "UltraGym & Fitness - Azahares")
TRAINING_USER = os.getenv("TRAINING_USER")
TRAINING_PASS = os.getenv("TRAINING_PASS")

# Perfil persistente (solo para local)
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


def tg_dump_debug(page, tag="debug"):
    try:
        page.screenshot(path=str(ARTIFACTS_DIR / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        (ARTIFACTS_DIR / f"{tag}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


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


def tg_detect_turnstile_block(page) -> bool:
    """Detecta bloqueo REAL de Cloudflare/Turnstile (no solo que exista la palabra)."""
    try:
        url = (page.url or "").lower()

        # Si te mandó a un challenge explícito
        if "cdn-cgi/challenge" in url or "challenge" in url and "cdn-cgi" in url:
            return True

        # Si existe el widget de turnstile visible (señal fuerte)
        if page.locator("iframe[src*='turnstile' i]").count() > 0:
            # OJO: a veces el iframe existe sin bloquear; confirmamos con body/texto típico
            body = (page.inner_text("body") or "").lower()
            if "verify" in body or "verifica" in body or "checking your browser" in body:
                return True

        # Señales típicas en el DOM cuando te bloquearon
        html = (page.content() or "").lower()
        strong_signals = [
            "checking your browser",
            "verifying you are human",
            "attention required",
            "cf-challenge",
            "cf-turnstile-response",
            "captcha",
            "ray id",
        ]
        if any(s in html for s in strong_signals):
            return True

        # Error específico que tú viste (600010) → SOLO si aparece tal cual
        if "600010" in html:
            return True

    except Exception:
        return False

    return False


# ================= Frames (PowerBI) =================
def tg_get_pbi_frame(page, timeout_ms=240_000):
    """
    Espera el iframe embed de PowerBI.
    En Actions a veces primero es about:blank y luego navega a app.powerbi.com/reportEmbed.
    """
    deadline = time.time() + timeout_ms / 1000
    last_iframes = []
    last_frames = []

    while time.time() < deadline:
        # 1) Buscar por page.frames
        last_frames = []
        for fr in page.frames:
            u = (fr.url or "").lower()
            last_frames.append(u[:180])
            if "app.powerbi.com" in u and ("reportembed" in u):
                return fr

        # 2) Buscar iframe DOM y tomar content_frame navegada
        last_iframes = []
        try:
            iframes = page.locator("iframe").all()
            for i in range(min(len(iframes), 25)):
                el = iframes[i]
                src = (el.get_attribute("src") or "").lower()
                title = el.get_attribute("title") or ""
                name = el.get_attribute("name") or ""
                last_iframes.append(f"{src[:160]} | title={title[:40]} | name={name[:40]}")

                if "powerbi" in src or "reportembed" in src:
                    cf = el.content_frame()
                    if cf:
                        u2 = (cf.url or "").lower()
                        if "app.powerbi.com" in u2 and "reportembed" in u2:
                            return cf
        except Exception:
            pass

        page.wait_for_timeout(1000)

    print("❌ No encontré el frame de PowerBI por selector ni por page.frames.")
    print("Iframes vistos (primeros 15):")
    for s in last_iframes[:15]:
        print("  -", s)
    print("Frames vistos por page.frames (primeros 15):")
    for s in last_frames[:15]:
        print("  -", s)

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


def tg_wait_for_pbi_loaded(page, timeout_ms=120_000) -> bool:
    """Señal: existe iframe con src de powerbi o frames con app.powerbi.com/reportEmbed."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if page.locator("iframe[src*='powerbi' i], iframe[src*='reportEmbed' i]").count() > 0:
                return True
        except Exception:
            pass

        for fr in page.frames:
            u = (fr.url or "").lower()
            if "app.powerbi.com" in u and "reportembed" in u:
                return True

        page.wait_for_timeout(1000)

    return False


def tg_find_frame_with_pbi(page, timeout_ms=180_000):
    """Devuelve el frame donde vive PowerBI embebido."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for fr in page.frames:
            try:
                u = (fr.url or "").lower()
                if "app.powerbi.com" in u and "reportembed" in u:
                    return fr
                if fr.locator("input.date-slicer-input").count() >= 1:
                    return fr
            except Exception:
                pass
        page.wait_for_timeout(400)
    return page.main_frame


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

    page.wait_for_timeout(1500)

    if tg_detect_turnstile_block(page):
        page.screenshot(path=str(ARTIFACTS_DIR / "tg_turnstile.png"), full_page=True)

        # Si estás en headless (o sea SHOW_BROWSER=0), ahí sí aborta:
        if not SHOW_BROWSER:
            raise RuntimeError("[TG] Cloudflare/Turnstile bloqueó el login (headless).")

        # Si estás en local con browser visible, solo avisa:
        print("⚠ [TG] Se detectó posible challenge/turnstile, pero como estás en modo visible continuaré.")


    page.wait_for_load_state("domcontentloaded", timeout=90_000)
    page.wait_for_timeout(1200)
    tg_close_annoying_modals(page)

    # Selección de centro (si aparece)
    try:
        print("➡ [TG] Seleccionando centro...")
        select_box = page.locator(
            "div[role='combobox'], .ant-select, nz-select, div.ant-select-selector"
        ).first
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


def tg_ensure_logged_in(page):
    """Valida login y loguea si hace falta."""
    def looks_like_login() -> bool:
        try:
            if "/auth" in (page.url or ""):
                return True
            if page.locator("input[type='password']").count() > 0:
                return True
            if page.get_by_role("button", name="ACCEDER").count() > 0:
                return True
        except Exception:
            pass
        return False

    if looks_like_login():
        tg_login_auto(page)
        return

    # Validación extra: ir al reporte; si te manda a auth, loguear
    try:
        page.goto(TRAINING_REPORT_WORKOUT_URL, timeout=90_000)
        page.wait_for_load_state("domcontentloaded", timeout=90_000)
        page.wait_for_timeout(800)
        tg_close_annoying_modals(page)

        if "/auth" in (page.url or "") or page.locator("input[type='password']").count() > 0:
            tg_login_auto(page)
    except Exception:
        page.goto(TRAINING_LOGIN_URL, timeout=90_000)
        page.wait_for_load_state("domcontentloaded", timeout=90_000)
        tg_login_auto(page)


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

    try:
        fr = tg_get_pbi_frame(page, timeout_ms=240_000)
    except Exception as e:
        print(f"⚠ [TG] No vi el iframe PBI aún. Reload y reintento... {e}")
        try:
            page.reload(wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2500)
        except Exception:
            pass
        fr = tg_get_pbi_frame(page, timeout_ms=240_000)

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
    "Valoración",
]


def tg_wait_refresh_until_table(page, timeout_ms=240_000):
    print("⏳ [TG] Esperando a que la tabla se refresque...")
    fr = tg_get_pbi_frame(page, timeout_ms=240_000)

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


# ================= Export (PowerBI) =================
def tg_export_table(page, end_date: date):
    tg_close_annoying_modals(page)
    fr = tg_find_frame_with_pbi(page, timeout_ms=180_000)

    def _find_export_modal_anywhere(timeout_ms=20_000):
        deadline = time.time() + timeout_ms / 1000

        while time.time() < deadline:
            for f in page.frames:
                try:
                    btn = f.locator(
                        "button[data-testid='export-btn'], button[aria-label='Exportar'], button:has-text('Exportar')"
                    ).first
                    if btn.count():
                        try:
                            btn.wait_for(state="attached", timeout=1000)
                            dlg = btn.locator("xpath=ancestor::div[@role='dialog'][1]").first
                            if dlg.count():
                                return f, dlg, btn
                            return f, None, btn
                        except Exception:
                            pass

                    title = f.get_by_text("¿Qué datos quiere exportar?", exact=False).first
                    if title.count():
                        try:
                            title.wait_for(state="attached", timeout=1000)
                            dlg = f.locator("div[role='dialog']").filter(has=title).first
                            if dlg.count():
                                btn2 = dlg.locator(
                                    "button[data-testid='export-btn'], button[aria-label='Exportar'], button:has-text('Exportar')"
                                ).first
                                if btn2.count():
                                    return f, dlg, btn2
                                return f, dlg, None
                            return f, None, None
                        except Exception:
                            pass

                except Exception:
                    continue

            page.wait_for_timeout(200)

        return None, None, None

    def _btn_disabled_fast(btn) -> bool:
        try:
            return btn.evaluate(
                """
                (b) => {
                  const d = b.getAttribute('disabled');
                  const ad = b.getAttribute('aria-disabled');
                  const cls = (b.className || '').toString();
                  return !!d || ad === 'true' || cls.includes('disabled');
                }
                """
            )
        except Exception:
            return False

    def _ensure_design_actual_selected(modal_frame, modal_root):
        root = modal_root if modal_root is not None else modal_frame

        def _selected_is_design():
            try:
                design_card = root.locator(
                    "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]"
                ).first
                if design_card.count():
                    if design_card.locator(
                        "[role='radio'][aria-checked='true'], input[type='radio']:checked, .mat-radio-checked"
                    ).count() > 0:
                        return True
            except Exception:
                pass
            return False

        if _selected_is_design():
            return True

        radio_candidates = [
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]//*[@role='radio']",
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]/ancestor::*[self::div or self::section][1]//input[@type='radio']",
            "xpath=//*[contains(normalize-space(.),'Datos con diseño actual')]",
        ]

        for sel in radio_candidates:
            try:
                loc = root.locator(sel).first
                if loc.count():
                    loc.click(force=True, timeout=3000)
                    page.wait_for_timeout(300)
                    if _selected_is_design():
                        return True
            except Exception:
                pass

        return _selected_is_design()

    # 0) asegurar que estamos en tabla/visual
    print("🧭 [TG] Asegurando tabla visible...")
    anchor = None
    for t in TABLE_HEADER_HINTS:
        loc = fr.get_by_text(t, exact=False)
        if loc.count():
            anchor = loc.first
            break
    if not anchor:
        raise RuntimeError("[TG] No encontré headers de tabla dentro del frame PBI.")

    anchor.scroll_into_view_if_needed()
    page.wait_for_timeout(400)

    # 1) localizar contenedor del visual
    print("➡ [TG] Localizando contenedor del visual (PowerBI)...")
    visual = anchor.locator("xpath=ancestor-or-self::*[@data-visual-id][1]")
    if visual.count() == 0:
        visual = anchor.locator("xpath=ancestor-or-self::div[contains(@class,'visualContainer')][1]")
    if visual.count() == 0:
        raise RuntimeError("[TG] No pude resolver el contenedor del visual.")

    # 2) si modal ya está abierto, no abras menú
    modal_frame, modal_root, export_btn = _find_export_modal_anywhere(timeout_ms=1500)
    if modal_frame:
        print("✅ [TG] Modal ya estaba visible; salto abrir menú.")
    else:
        # activar toolbar
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

            print("➡ [TG] Esperando modal exportación ...")
            modal_frame, modal_root, export_btn = _find_export_modal_anywhere(timeout_ms=20_000)
            if modal_frame:
                print("✅ [TG] Modal exportación detectado.")
                break

            print("⚠ [TG] No apareció modal aún; reintento…")
            page.wait_for_timeout(650)
        else:
            raise RuntimeError("No logré abrir el modal de exportación tras 5 intentos.")

    # 3) asegurar export_btn
    if export_btn is None or export_btn.count() == 0:
        if modal_root is not None:
            export_btn = modal_root.locator(
                "button[data-testid='export-btn'], button[aria-label='Exportar'], button:has-text('Exportar')"
            ).first
        else:
            export_btn = modal_frame.locator(
                "button[data-testid='export-btn'], button[aria-label='Exportar'], button:has-text('Exportar')"
            ).first

    if export_btn is None or export_btn.count() == 0:
        raise RuntimeError("[TG] Detecté el modal pero no pude resolver el botón Exportar.")

    export_btn.wait_for(state="attached", timeout=60_000)

    # 4) forzar "Datos con diseño actual"
    try:
        _ensure_design_actual_selected(modal_frame, modal_root)
    except Exception:
        pass

    # 5) esperar que el botón no esté disabled (sin is_enabled)
    end_wait = time.time() + 25
    while time.time() < end_wait:
        if not _btn_disabled_fast(export_btn):
            break
        page.wait_for_timeout(250)

    # 6) click export + esperar descarga
    print("⬇️ [TG] Click Exportar + esperando descarga...")
    download = None
    last_err = None

    for attempt in range(1, 6):
        try:
            print(f"➡ [TG] Click Exportar intento {attempt}/5...")

            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dlinfo:
                try:
                    export_btn.click(timeout=3000, force=True)
                except Exception:
                    pass
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

    browser = None
    context = None

    with sync_playwright() as p:
        if SHOW_BROWSER:
            # Local: perfil persistente (Edge)
            context = p.chromium.launch_persistent_context(
                user_data_dir=TG_PROFILE_DIR,
                channel="msedge",
                headless=False,
                accept_downloads=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=msEdgeTranslate",
                ],
            )
            _stealth(context)
            page = context.new_page()
        else:
            # Actions: SIN perfil persistente (menos flakiness)
            browser = p.chromium.launch(
                channel="msedge",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=msEdgeTranslate",
                    "--enable-unsafe-swiftshader",
                ],
            )
            context = browser.new_context(accept_downloads=True)
            _stealth(context)
            page = context.new_page()

        page.set_default_timeout(30_000)
        page.set_default_navigation_timeout(90_000)
        page.on("console", lambda m: print(f"🧾 [console] {m.type}: {m.text}"))

        try:
            tg_goto_workout(page)
            tg_ensure_logged_in(page)
            tg_goto_workout(page)

            if not tg_wait_for_pbi_loaded(page, timeout_ms=90_000):
                try:
                    page.screenshot(path=str(ARTIFACTS_DIR / "tg_no_pbi.png"), full_page=True)
                except Exception:
                    pass
                raise RuntimeError("[TG] No cargó el embed de PowerBI (posible bloqueo Turnstile/Cloudflare).")

            tg_set_dates_inputs_js(page, start_d, end_d)
            tg_wait_refresh_until_table(page, timeout_ms=240_000)
            tg_export_table(page, end_d)

        except Exception as e:
            print("💥 [TG] ERROR:", repr(e))
            tg_dump_debug(page, tag="tg_fail")
            raise
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
