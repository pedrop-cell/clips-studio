# Clipador para Windows: prepara tudo na primeira vez e liga o monitor.
# Chamado por Iniciar-Clipador.bat. Rodar de novo e seguro: so instala o que falta.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root ".venv-clipador"
$Py = Join-Path $Venv "Scripts\python.exe"

function Passo($texto) { Write-Host ""; Write-Host "==> $texto" -ForegroundColor Cyan }
function Falha($texto) { Write-Host ""; Write-Host "ERRO: $texto" -ForegroundColor Red; exit 1 }
function Atualizar-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Tem($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Passo "Conferindo o Python"
$PyBase = $null
foreach ($c in @("py -3.12", "py -3.11", "python")) {
    $parts = $c.Split(" ")
    try {
        $v = & $parts[0] $parts[1..9] -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
        if ($v -eq "True") { $PyBase = $parts; break }
    } catch {}
}
if (-not $PyBase) {
    if (-not (Tem "winget")) { Falha "Instale o Python 3.12 em python.org (marque 'Add to PATH') e abra de novo." }
    Write-Host "Instalando o Python 3.12 (uma vez so)..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
    Atualizar-Path
    $PyBase = @("py", "-3.12")
}

Passo "Conferindo o ffmpeg"
if (-not (Tem "ffmpeg")) {
    if (-not (Tem "winget")) { Falha "Instale o ffmpeg (winget install Gyan.FFmpeg) e abra de novo." }
    Write-Host "Instalando o ffmpeg (uma vez so)..."
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements --silent
    Atualizar-Path
    if (-not (Tem "ffmpeg")) { Falha "O ffmpeg foi instalado mas o Windows ainda nao achou. Feche e abra o Iniciar-Clipador.bat de novo." }
}

Passo "Preparando o ambiente do Clipador"
if (-not (Test-Path $Py)) {
    & $PyBase[0] $PyBase[1..9] -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Falha "Nao consegui criar o ambiente Python." }
}
& $Py -m pip install --quiet --disable-pip-version-check --upgrade -r (Join-Path $Root "windows\requirements-live.txt")
if ($LASTEXITCODE -ne 0) { Falha "Nao consegui instalar os pacotes. Confira a internet e rode de novo." }
$env:Path = (Join-Path $Venv "Scripts") + ";" + $env:Path

Passo "Conferindo o Telegram"
& $Py -m live_clipper.setup --folder $Root --if-needed
if ($LASTEXITCODE -ne 0) { Falha "A configuracao do Telegram nao terminou." }

Passo "Esperando o app Clips Kitty"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
        if ($r.ok) { $ok = $true; break }
    } catch {}
    if ($i -eq 0) { Write-Host "Abra o app Clips Kitty (Microsoft Store) e deixe aberto. Estou esperando..." -ForegroundColor Yellow }
    Start-Sleep -Seconds 5
}
if (-not $ok) { Falha "O app Clips Kitty nao abriu em 5 minutos. Abra o app e rode de novo." }

Passo "Verificando tudo"
& $Py -m live_clipper --config live_clipper.yaml --check
if ($LASTEXITCODE -ne 0) { Falha "Alguma verificacao falhou (veja acima)." }

Passo "Monitor ligado. Deixe esta janela aberta; feche para parar."
& $Py -m live_clipper --config live_clipper.yaml @args
