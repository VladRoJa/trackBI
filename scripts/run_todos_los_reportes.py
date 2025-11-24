# scripts/run_todos_los_reportes.py

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]  # carpeta TRACK BI
SCRIPTS_DIR = Path(__file__).resolve().parent

def run_script(nombre):
    ruta = SCRIPTS_DIR / nombre
    print(f"\n🔹 Ejecutando {nombre}...\n")
    result = subprocess.run(
        [sys.executable, str(ruta)],
        capture_output=True,
        text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ {nombre} terminó con error:")
        print(result.stderr)
        raise SystemExit(result.returncode)
    print(f"✅ {nombre} terminado OK.\n")

def main():
    # 1) Dirección + KPIs
    run_script("reporte_direccion_ingresos.py")

    # 2) Descargas de Dirección (corte caja, venta total, cargos recurrentes)
    run_script("reporte_descargas.py")

if __name__ == "__main__":
    main()
