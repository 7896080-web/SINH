<#
    Обновление уже установленного sync_admin на Windows.
    Запускать ПОСЛЕ того, как заменили файлы проекта (новый код) в C:\sync_admin.
    Делает: зависимости -> миграции -> перезапуск служб -> проверка /health.

    Запуск (PowerShell от администратора):
        powershell -ExecutionPolicy Bypass -File C:\sync_admin\deploy\update_windows.ps1
#>
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "[X] Не найден .venv — сначала установка (install_windows.ps1)." -ForegroundColor Red
    exit 1
}

Write-Host "[*] Зависимости (на случай новых)..." -ForegroundColor Cyan
& $py -m pip install -r (Join-Path $root "requirements.txt") | Out-Null

Write-Host "[*] Миграции базы..." -ForegroundColor Cyan
& $py -m alembic upgrade head

Write-Host "[*] Перезапуск служб..." -ForegroundColor Cyan
& nssm restart sync_admin_web   | Out-Null
& nssm restart sync_admin_worker | Out-Null
Start-Sleep -Seconds 3

Write-Host "[*] Проверка http://127.0.0.1:8000/health ..." -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 15
    Write-Host "[OK] health HTTP $($r.StatusCode)" -ForegroundColor Green
    Write-Host $r.Content
} catch {
    Write-Host "[!] /health не ответил успешно — смотрите logs\web.err.log и logs\worker.err.log" -ForegroundColor Yellow
}

Write-Host "[OK] Обновление завершено." -ForegroundColor Green
