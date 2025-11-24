@echo off
echo Iniciando run_reportes.bat ...

REM Ir a la carpeta donde están los scripts y el .venv
cd /d "C:\Users\Vladimir\Documents\Reportes\track\TRACK BI\scripts"

echo.
echo Directorio actual:
cd
echo.

echo Si ves este mensaje, el BAT SI está corriendo.
echo.

REM ===== Activar entorno virtual (.venv dentro de scripts) =====
if exist ".venv\Scripts\activate.bat" (
    echo Activando entorno virtual en .venv...
    call ".venv\Scripts\activate.bat"
) else (
    echo No se encontró ".venv\Scripts\activate.bat".
    echo Revisa que el entorno virtual exista en:
    echo   C:\Users\Vladimir\Documents\Reportes\track\TRACK BI\scripts\.venv
    pause
    goto :EOF
)

echo.
echo ========= Ejecutando reporte_direccion_ingresos.py =========
python "reporte_direccion_ingresos.py"
if errorlevel 1 (
    echo.
    echo ❌ Error al ejecutar reporte_direccion_ingresos.py
    pause
    goto :EOF
)

echo.
echo ========= Ejecutando reporte_descargas.py =========
python "reporte_descargas.py"
if errorlevel 1 (
    echo.
    echo ❌ Error al ejecutar reporte_descargas.py
    pause
    goto :EOF
)

echo.
echo ✅ Todos los reportes terminaron sin errores.
exit