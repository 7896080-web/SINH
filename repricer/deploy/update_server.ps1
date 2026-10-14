<#
.SYNOPSIS
    Сервер: обновление «Репрайсера» — остановка службы -> копия базы -> зависимости ->
    миграции -> тесты -> запуск службы -> проверка, что отвечает.

.DESCRIPTION
    Новые файлы уже в C:\repricer (патч rpN). Любой неуспех — код 1, и служба при
    красных тестах НЕ запускается: старый код с новой схемой базы хуже паузы.

    PowerShell от администратора:
        powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\update_server.ps1
#>
[CmdletBinding()]
param([int]$WebPort = 8002, [string]$Service = "repricer")
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — сначала install_server.ps1" }
if (-not (Get-Service -Name $Service -ErrorAction SilentlyContinue)) { Fail "Нет службы $Service — сначала install_server.ps1" }

Info "Останавливаю службу $Service"
& nssm stop $Service | Out-Null
Start-Sleep -Seconds 2

Info "Копия базы до обновления"
& $venvPy -c "import priceapp; from priceapp import backup; e=backup.make_backup(); print(e or 'ok'); raise SystemExit(1 if e and 'SQLite' not in e else 0)"
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
& nssm start $Service | Out-Null
$url = "http://127.0.0.1:$WebPort"
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch { Start-Sleep -Seconds 1 }
}
if (-not $up) { Fail "Служба не отвечает на $url — журнал: $Root\logs\repricer.err.log (открывать с -Encoding UTF8)" }
Ok "Обновление завершено, программа отвечает: $url"
