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

# Копия базы ПЕРЕД миграцией — это единственный момент, когда она точно нужна:
# суточная копия может быть двадцатичасовой давности, а неудачная миграция меняет
# схему необратимо. Копия снимается штатным механизмом SQLite на живой базе, службы
# останавливать не надо.
Write-Host "[*] Копия базы перед миграцией..." -ForegroundColor Cyan
& $py (Join-Path $root "scripts\backup_db.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] Копия не снялась — обновление остановлено. Разберитесь с бэкапом:" -ForegroundColor Red
    Write-Host "    место на диске, права на C:\sync_admin\backups, переменная BACKUP_DIR." -ForegroundColor Red
    exit 1
}

Write-Host "[*] Миграции базы..." -ForegroundColor Cyan
& $py -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] Миграция НЕ прошла. Службы не перезапускались — бой работает" -ForegroundColor Red
    Write-Host "    на старом коде, схема осталась прежней или наполовину новой." -ForegroundColor Red
    Write-Host "    Копия базы снята шагом выше: C:\sync_admin\backups." -ForegroundColor Red
    Write-Host "    Посмотрите, до какой ревизии дошли:  .\.venv\Scripts\python.exe -m alembic current" -ForegroundColor Red
    exit 1
}

Write-Host "[*] Тесты..." -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Тесты не прошли. Службы НЕ перезапущены — код на диске новый," -ForegroundColor Yellow
    Write-Host "    но работает ещё старый. Разберитесь до перезапуска." -ForegroundColor Yellow
    exit 1
}

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
