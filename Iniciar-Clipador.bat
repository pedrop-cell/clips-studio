@echo off
rem Clipador: instala o que falta na primeira vez e liga o monitor de lives.
rem Deixe o app Clips Kitty aberto antes. Feche esta janela para parar.
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\clipador.ps1" %*
echo.
pause
