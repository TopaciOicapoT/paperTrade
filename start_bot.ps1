# start_bot.ps1
# Wrapper que mantiene el paper trader corriendo.
# Se reinicia automáticamente si el proceso termina por error.
# Para detenerlo limpiamente: Ctrl+C o cierra la ventana.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python    = Join-Path $scriptDir ".venv\Scripts\python.exe"
$main      = Join-Path $scriptDir "main.py"
$logDir    = Join-Path $scriptDir "logs"
$restartDelay = 30   # segundos antes de reintentar tras un crash

if (-not (Test-Path $python)) {
    Write-Error "No se encontró el entorno virtual en $python"
    exit 1
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Write-Host "=== autoTrading paper trader ===" -ForegroundColor Cyan
Write-Host "Presiona Ctrl+C para detener." -ForegroundColor Yellow

# Capturar Ctrl+C a nivel de script para garantizar salida limpia (código 0)
# Sin esto, PowerShell termina el script antes de leer el exit code de Python
trap {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "`n[$timestamp] Paper trader detenido por el usuario." -ForegroundColor Yellow
    exit 0
}

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] Iniciando paper trader..." -ForegroundColor Green

    & $python $main paper

    $exitCode = $LASTEXITCODE
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    if ($exitCode -eq 0) {
        Write-Host "[$timestamp] El proceso terminó limpiamente (código 0). Deteniendo." -ForegroundColor Green
        break
    }

    Write-Host "[$timestamp] El proceso terminó con código $exitCode. Reintentando en $restartDelay s..." -ForegroundColor Yellow
    Start-Sleep -Seconds $restartDelay
}
