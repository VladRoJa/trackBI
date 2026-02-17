import re
from pathlib import Path
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]  # TRACK BI
RUTINAS_DIR = BASE_DIR / "data" / "rutinas"

GASCA_PATH = RUTINAS_DIR / "gasca_ventas_nuevas_socios.xlsx"
TG_PATH    = RUTINAS_DIR / "tg_workout.xlsx"

OUT_SPLIT_DIR = RUTINAS_DIR / "sucursales"
OUT_SPLIT_DIR.mkdir(parents=True, exist_ok=True)

def _norm_email(x):
    if pd.isna(x):
        return ""
    return str(x).strip().lower()

def _safe_filename(name: str) -> str:
    name = str(name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "SIN_SUCURSAL"

def main():
    if not GASCA_PATH.exists():
        raise FileNotFoundError(f"No existe: {GASCA_PATH}")
    if not TG_PATH.exists():
        raise FileNotFoundError(f"No existe: {TG_PATH}")

    # ================= 1) Leer archivos =================
    gasca = pd.read_excel(GASCA_PATH, dtype=str)
    tg = pd.read_excel(TG_PATH, dtype=str)

    # ================= 2) Normalizar emails =================
    if "Email" not in gasca.columns:
        raise RuntimeError("gasca_ventas_nuevas_socios no tiene columna Email")
    if "Email" not in tg.columns:
        raise RuntimeError("tg_workout no tiene columna Email")

    gasca["Email_norm"] = gasca["Email"].apply(_norm_email)
    tg["Email_norm"] = tg["Email"].apply(_norm_email)

    # ================= 3) Limpiar TG: quitar Técnico == automatico =================
    if "Técnico" not in tg.columns:
        raise RuntimeError("tg_workout no tiene columna 'Técnico'")

    import unicodedata

    def _norm_text(x: str) -> str:
        s = "" if pd.isna(x) else str(x).strip().lower()
        # quitar acentos: "automático" -> "automatico"
        s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
        # compactar espacios
        s = re.sub(r"\s+", " ", s).strip()
        return s

    tg["Tecnico_norm"] = tg["Técnico"].apply(_norm_text)

    # filtra "automatico" y variantes
    mask_auto = tg["Tecnico_norm"].eq("automatico") | tg["Tecnico_norm"].str.contains(r"\bautomat", regex=True)
    tg = tg.loc[~mask_auto].copy()


    # ================= 4) Resolver duplicados por email (elige el más reciente por Fecha si existe) =================
    # Si "Fecha" existe, ordena por Fecha (intenta parsear) y toma el último
    if "Fecha" in tg.columns:
        tg["_fecha_dt"] = pd.to_datetime(tg["Fecha"], errors="coerce", dayfirst=True)
        tg = tg.sort_values(["Email_norm", "_fecha_dt"])
        tg_map = tg.drop_duplicates("Email_norm", keep="last")[["Email_norm", "Técnico"]]
    else:
        tg_map = tg.drop_duplicates("Email_norm", keep="first")[["Email_norm", "Técnico"]]

    tg_map = tg_map.rename(columns={"Técnico": "Instructor trainingym_lookup"})

    # ================= 5) Asegurar columnas destino en Gasca =================
    # Deben llamarse EXACTO como las tienes:
    col_instructor = "instructor Trainingym"
    col_rutina = "Rutina"

    if col_instructor not in gasca.columns:
        gasca[col_instructor] = ""
    if col_rutina not in gasca.columns:
        gasca[col_rutina] = ""

    # ================= 6) Merge / cruce =================
    gasca = gasca.merge(tg_map, on="Email_norm", how="left")

    # Llenar instructor: si no hay match => N/D
    gasca[col_instructor] = gasca["Instructor trainingym_lookup"].fillna("N/D")

    # Rutina según instructor
    gasca[col_rutina] = gasca[col_instructor].apply(
        lambda x: "sin rutina" if str(x).strip().upper() == "N/D" else "con rutina"
    )

    # Quitar columnas auxiliares
    gasca = gasca.drop(columns=[c for c in ["Email_norm", "Instructor trainingym_lookup"] if c in gasca.columns])

    # ================= 7) Guardar Gasca “ya cruzado” (valores finales) =================
    cruzado_path = RUTINAS_DIR / "gasca_ventas_nuevas_socios_cruzado.xlsx"
    gasca.to_excel(cruzado_path, index=False)

    # ================= 8) Split por sucursal =================
    if "Sucursal" not in gasca.columns:
        raise RuntimeError("gasca_ventas_nuevas_socios no tiene columna Sucursal")

    for sucursal, df_suc in gasca.groupby("Sucursal", dropna=False):
        fname = _safe_filename(sucursal)
        out_path = OUT_SPLIT_DIR / f"Socios_con_y_sin_rutina__{fname}.xlsx"
        df_suc.to_excel(out_path, index=False)

    print(f"✅ Cruzado: {cruzado_path.name}")
    print(f"✅ Archivos por sucursal en: {OUT_SPLIT_DIR}")

if __name__ == "__main__":
    main()
