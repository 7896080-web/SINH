<#
.SYNOPSIS
    Сервер: обновление «Финансов площадок» — остановка службы -> копия базы ->
    зависимости -> миграции -> тесты -> запуск службы -> проверка, что отвечает.

.DESCRIPTION
    Новые файлы уже в C:\mpfinance (пакет mpfinance_finN.ps1). Любой неуспех —
    код 1, и служба при красных тестах НЕ запускается: старый код с новой схемой
    базы хуже паузы.

    PowerShell от администратора:
        powershell -ExecutionPolicy Bypass -File C:\mpfinance\deploy\update_server.ps1 -Tag finN

    -Tag — версия, которую СОБИРАЛИСЬ поставить. Пакет пишет deploy\INSTALLED_TAG
    после того, как все файлы записаны и сверены; не совпало — накат
    останавливается ДО остановки службы: иначе он честно отработал бы на старом
    коде и кончился зелёным (урок sync_admin 23.09).
#>
[CmdletBinding()]
param([int]$WebPort = 8003, [string]$Service = "mpfinance", [string]$Tag = "")
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — сначала install_server.ps1" }
if (-not (Get-Service -Name $Service -ErrorAction SilentlyContinue)) { Fail "Нет службы $Service — сначала install_server.ps1" }

$tagFile = Join-Path $Root "deploy\INSTALLED_TAG"
$installed = if (Test-Path $tagFile) { (Get-Content $tagFile -TotalCount 1).Trim() } else { "" }
if ($Tag) {
    if ($installed -ne $Tag) { Fail "На диске версия '$installed', а ставим '$Tag' — пакет $Tag не применён. Служба не тронута." }
    Ok "Версия на диске: $installed"
} else {
    Info "Версия на диске: $(if ($installed) { $installed } else { 'неизвестна' }) (проверка: -Tag finN)"
}

Info "Останавливаю службу $Service"
& nssm stop $Service | Out-Null
Start-Sleep -Seconds 2

Info "Копия базы до обновления"
& $venvPy -c "import finapp; from finapp import backup; e=backup.make_backup(); print(e or 'ok'); raise SystemExit(1 if e and 'SQLite' not in e else 0)"
if ($LASTEXITCODE -ne 0) { & nssm start $Service | Out-Null; Fail "Копия базы не снялась — обновление остановлено, служба запущена на прежней версии" }

Info "Зависимости"
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой" }

Info "Миграции"
& $venvPy -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head завершился с ошибкой" }

Info "Тесты"
$saved = $env:DATABASE_URL
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
& $venvPy -m pytest -q -p no:cacheprovider
$rc = $LASTEXITCODE
if ($saved) { $env:DATABASE_URL = $saved }
if ($rc -ne 0) { Fail "Тесты красные — службу не запускаю" }

Info "Запуск службы $Service"
$startedUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss")
& nssm start $Service | Out-Null
$url = "http://127.0.0.1:$WebPort"
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch { Start-Sleep -Seconds 1 }
}
if (-not $up) { Fail "Служба не отвечает на $url — журнал: $Root\logs\mpfinance.err.log (открывать с -Encoding UTF8)" }

# Страница отвечает — ещё не значит, что работает фон: отметки в базе остались от
# ПРЕЖНЕГО процесса. Ждём отметку обмена с 1С от нового процесса (каждые 30 с).
Info "Жду фоновые задания нового процесса"
$check = "import sys, finapp; from datetime import datetime; from finapp.database import SessionLocal; " +
         "from finapp.models import WorkerHeartbeat; db = SessionLocal(); r = db.get(WorkerHeartbeat, 'onec_exchange'); " +
         "sys.exit(0 if r and r.last_run_at and r.last_run_at >= datetime.fromisoformat(sys.argv[1]) else 1)"
$alive = $false
# PowerShell 5.1 при "Stop" превращает любой вывод python в stderr в исключение и рвёт накат.
$ErrorActionPreference = "Continue"
for ($i = 0; $i -lt 30 -and -not $alive; $i++) {
    & $venvPy -c $check $startedUtc 2>$null
    if ($LASTEXITCODE -eq 0) { $alive = $true } else { Start-Sleep -Seconds 5 }
}
$ErrorActionPreference = "Stop"
if (-not $alive) { Fail "Страница отвечает, но фон нового процесса за 150 с не отметился — журнал: $Root\logs\mpfinance.err.log" }
Ok "Обновление завершено ($installed), программа отвечает и фон работает: $url"
