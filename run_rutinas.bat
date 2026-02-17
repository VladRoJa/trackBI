@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ===== Ir a la carpeta del proyecto (donde está este .bat) =====
cd /d "%~dp0"

echo.
echo ================================
echo   TRACK BI - Rutinas Pipeline
echo ================================
echo Proyecto: %cd%
echo.

REM ===== Activar venv =====
if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
) else (
  echo [ERROR] No encontro .venv\Scripts\activate.bat
  echo Abre este .bat dentro de la raiz del proyecto TRACK BI.
  pause
  exit /b 1
)

REM ===== Cargar variables desde .env (simple, sin comillas/espacios raros) =====
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "K=%%A"
    set "V=%%B"
    if not "!K!"=="" (
      if /i not "!K:~0,1!"=="#" (
        set "!K!=!V!"
      )
    )
  )
) else (
  echo [WARN] No encontre .env en la raiz. Continuo de todos modos...
)

REM ===== Carpetas necesarias =====
if not exist "logs" mkdir "logs"
if not exist "scripts\artifacts" mkdir "scripts\artifacts"
if not exist "data\rutinas\raw" mkdir "data\rutinas\raw"
if not exist "data\rutinas\sucursales" mkdir "data\rutinas\sucursales"

REM ===== Log =====
set "TS=%DATE%_%TIME%"
set "TS=%TS::=-%"
set "TS=%TS:/=-%"
set "TS=%TS: =_%"
set "LOG=logs\rutinas_pipeline_%TS%.log"

echo Log: %LOG%
echo.

REM ===== Ejecutar 1) TG workout export =====
echo [1/3] tg_workout_export.py
python -u "scripts\tg_workout_export.py" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] Fallo tg_workout_export.py
  echo Revisa: %LOG%
  pause
  exit /b 1
)

REM ===== Ejecutar 2) Socios nuevos (Gasca) =====
echo [2/3] Socios_nuevos_gasca.py
python -u "scripts\Socios_nuevos_gasca.py" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] Fallo Socios_nuevos_gasca.py
  echo Revisa: %LOG%
  pause
  exit /b 1
)

REM ===== Ejecutar 3) Cruce + dividir sucursales =====
echo [3/3] cruzar_rutinas_y_dividir_sucursales.py
python -u "scripts\cruzar_rutinas_y_dividir_sucursales.py" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] Fallo cruzar_rutinas_y_dividir_sucursales.py
  echo Revisa: %LOG%
  pause
  exit /b 1
)

echo.
echo ✅ Listo. Pipeline completado OK.
echo Revisa salida en data\rutinas\ y log en %LOG%
echo.
pause
exit /b 0
