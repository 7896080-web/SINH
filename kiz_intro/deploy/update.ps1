<#
.SYNOPSIS
    Обновление «Ввода в оборот»: остановка → копия базы → зависимости → тесты → запуск.

.DESCRIPTION
    Новые файлы уже лежат в C:\kiz_intro (кроме .env, kiz.db, backups\, logs\).
    Сервер останавливается по ПОРТУ, а не по имени процесса: при Python из
    Microsoft Store сервер живёт не в python.exe (урок «Маркировки», 05.10.2026).
    Не остановился (запущен от администратора) — обновление стоп, база цела.

    powershell -ExecutionPolicy Bypass -File C:\kiz_intro\deploy\update.ps1
#>
[CmdletBinding()]
param([int]$Port = 8002)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — сначала install.ps1" }

Info "Останавливаю программу, если запущена"
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*kizapp.web:app*" -and $_.CommandLine -like "*--port $Port*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
try {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
} catch { }
$up = $true
for ($i = 0; $i -lt 20 -and $up; $i++) {
    try { Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Sleep -Milliseconds 500 }
    catch { $up = $false }
}
if ($up) { Fail "Программа на порту $Port не остановилась — закройте её (Диспетчер задач: python) и запустите обновление снова" }

$db = Join-Path $Root "kiz.db"
if (Test-Path $db) {
    Info "Копия базы до обновления"
    $dir = Join-Path $Root "backups"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $target = Join-Path $dir ("kiz-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".db")
    & $venvPy -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $db $target
    if ($LASTEXITCODE -ne 0) { Fail "Копия базы не снялась — обновление остановлено" }
    Ok "Копия: $target"
}

Info "Зависимости"
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой" }

Info "Тесты"
& $venvPy -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { Fail "Тесты красные — программу не запускаю" }

Info "Запуск"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "deploy\run.ps1") -Port $Port
if ($LASTEXITCODE -ne 0) { Fail "Программа не запустилась — смотрите logs\kiz.err.log" }
Ok "Обновление завершено"
