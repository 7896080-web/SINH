<#
.SYNOPSIS
    Обновление программы маркировки на рабочем компьютере: остановка -> копия
    базы -> зависимости -> миграции -> тесты -> запуск.

.DESCRIPTION
    Новые файлы должны уже лежать в C:\marking (кроме .env, marking.db, ssh\,
    backups\, logs\, tools\, rclone.conf, onec_archive\). Любой неуспех — код 1.

    powershell -ExecutionPolicy Bypass -File C:\marking\deploy\update_workstation.ps1
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
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — сначала install_workstation.ps1" }

# Остановить запущенную программу: миграция на живой базе и старый код в
# памяти — плохое сочетание. Ищем именно наш uvicorn по порту в командной строке.
Info "Останавливаю программу, если запущена"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*markapp.main:app*" -and $_.CommandLine -like "*--port $WebPort*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

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
if ($rc -ne 0) { Fail "Тесты красные — программу не запускаю" }

# Иконка на уже созданных ярлыках. Ярлык заводит только установщик, и тем, кто
# поставил программу до появления иконки, обновление иначе её не принесёт.
# Меняем ТОЛЬКО иконку: путь запуска и порт в ярлыке не трогаем.
$icon = Join-Path $Root "deploy\marking.ico"
if (Test-Path $icon) {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Startup"))) {
        $lnk = Join-Path $dir "Маркировка.lnk"
        if (Test-Path $lnk) {
            $s = $shell.CreateShortcut($lnk)
            $s.IconLocation = "$icon,0"
            $s.Save()
        }
    }
}

Info "Запуск"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "deploy\run_marking.ps1") -Port $WebPort
if ($LASTEXITCODE -ne 0) { Fail "Программа не запустилась — смотрите logs\marking.err.log" }
Ok "Обновление завершено"
