<#
.SYNOPSIS
    Накат новой версии программы маркировки: копия базы -> зависимости ->
    миграции -> тесты -> перезапуск служб -> /health.

.DESCRIPTION
    Новые файлы программы должны уже лежать в C:\marking. Скрипт не трогает
    sync_admin. Любой неуспех — выход с кодом 1, а не «готово» жёлтым текстом.

    powershell -ExecutionPolicy Bypass -File C:\marking\deploy\update_marking.ps1
#>
[CmdletBinding()]
param([int]$WebPort = 8001)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — сначала install_marking.ps1" }

Info "Копия базы до обновления"
& $venvPy -c "import markapp; from markapp import backup; r=backup.make_backup(); print(r.path, r.error or 'ok'); raise SystemExit(1 if r.error else 0)"
if ($LASTEXITCODE -ne 0) { Fail "Копия базы не снялась — обновление остановлено" }

Info "Зависимости"
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой" }

Info "Миграции"
& $venvPy -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head завершился с ошибкой" }

Info "Тесты"
$saved = $env:DATABASE_URL
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
& $venvPy -m pytest -q
$rc = $LASTEXITCODE
if ($saved) { $env:DATABASE_URL = $saved }
if ($rc -ne 0) { Fail "Тесты красные — службы не перезапускаю" }

Info "Перезапуск служб"
& nssm restart marking_worker
if ($LASTEXITCODE -ne 0) { Fail "marking_worker не перезапустился" }
& nssm restart marking_web
if ($LASTEXITCODE -ne 0) { Fail "marking_web не перезапустился" }

# Первые секунды /health отвечает по отметкам СТАРОГО процесса — поэтому ждём
# честно, с опросом, а не три секунды.
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$WebPort/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if (-not $ok) { Fail "/health не зелёный за 150 с — смотрите C:\marking\logs\marking_*.err.log" }
Ok "Обновление завершено"
