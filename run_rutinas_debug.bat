@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
  call ".venv\Scripts\activate.bat"
) else (
  echo [ERROR] No encontro .venv\Scripts\activate.bat
  pause
  exit /b 1
)

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
)

if not exist "logs" mkdir "logs"
if not exist "scripts\artifacts" mkdir "scripts\artifacts"

set "TS=%DATE%_%TIME%"
set "TS=%TS::=-%"
set "TS=%TS:/=-%"
set "TS=%TS: =_%"
set "LOG=logs\rutinas_pipeline_%TS%_DEBUG.log"

echo Log: %LOG%
echo.

echo [1/3] tg_workout_export.py
python -u "scripts\tg_workout_export.py" 2>&1 | tee "%LOG%"
if errorlevel 1 goto :fail

echo [2/3] Socios_nuevos_gasca.py
python -u "scripts\Socios_nuevos_gasca.py" 2>&1 | tee -a "%LOG%"
if errorlevel 1 goto :fail

echo [3/3] cruzar_rutinas_y_dividir_sucursales.py
python -u "scripts\cruzar_rutinas_y_dividir_sucursales.py" 2>&1 | tee -a "%LOG%"
if errorlevel 1 goto :fail

echo ✅ Listo.
pause
exit /b 0

:fail
echo ❌ Fallo. Revisa %LOG%
pause
exit /b 1
