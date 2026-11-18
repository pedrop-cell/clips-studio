@echo off
rem Manda agora as lives de hoje dos streamers da lista pro app achar os cortes.
rem Deixe o app Clips Kitty aberto. Cada live demora no app; deixe a janela aberta.
rem Rode o Iniciar-Clipador.bat pelo menos uma vez antes.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv-clipador\Scripts\python.exe" (
  echo Rode o Iniciar-Clipador.bat primeiro, uma vez.
  pause
  exit /b 1
)
".venv-clipador\Scripts\python.exe" -m live_clipper.vods --config live_clipper.yaml
echo.
pause
