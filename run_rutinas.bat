@echo off
setlocal EnableExtensions
chcp 65001 >nul

echo ==============================
echo  TRACK BI - Rutinas Pipeline
echo ==============================
echo Proyecto: %~dp0
echo.

REM 1) Ir a la carpeta donde está este .bat (raíz del proyecto)
pushd "%~dp0"

REM 2) Activar venv si existe
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
  call "venv\Scripts\activate.bat"
) else (
  echo [WARN] No encontre .venv\Scripts\activate.bat ni venv\Scripts\activate.bat
  echo        Usare el python disponible en PATH.
)

REM 3) Validar python
where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] No hay python en PATH.
  goto :end
)

echo.
echo [1/3] tg_workout_export.py
python -u "scripts\tg_workout_export.py"
if errorlevel 1 goto :fail

echo.
echo [2/3] Socios_nuevos_gasca.py
python -u "scripts\Socios_nuevos_gasca.py"
if errorlevel 1 goto :fail

echo.
echo [3/3] cruzar_rutinas_y_dividir_sucursales.py
python -u "scripts\cruzar_rutinas_y_dividir_sucursales.py"
if errorlevel 1 goto :fail

echo.
echo ✅ Listo. Archivos generados en data\rutinas\
goto :end

:fail
echo.
echo ❌ Fallo el pipeline. Codigo de salida: %errorlevel%

:end
popd
echo.
pause
