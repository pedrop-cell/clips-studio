@echo off
rem Procura agora os videos de GTA 6 que estao viralizando e manda pro Telegram.
rem Rode o Iniciar-Clipador.bat pelo menos uma vez antes.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv-clipador\Scripts\python.exe" (
  echo Rode o Iniciar-Clipador.bat primeiro, uma vez.
  pause
  exit /b 1
)
".venv-clipador\Scripts\python.exe" -m live_clipper.trends --config live_clipper.yaml --once
echo.
pause
