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
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] nssm restart sync_admin_web вернул $LASTEXITCODE — служба не перезапущена." -ForegroundColor Red
    exit 1
}
& nssm restart sync_admin_worker | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] nssm restart sync_admin_worker вернул $LASTEXITCODE — служба не перезапущена." -ForegroundColor Red
    exit 1
}

# Ждём УСТОЙЧИВОГО «ok», а не трёх секунд.
#
# Трёх секунд не хватало по двум причинам сразу. Первая: самый короткий срок
# протухания в EXPECTED_INTERVAL_SECONDS — 120 секунд (recalc), а строки
# heartbeat перезапуск не трогает, так что первые две минуты /health отдаёт 200
# по ОТМЕТКАМ ОТ СТАРОГО ПРОЦЕССА — независимо от того, поднялся воркер или ушёл
# в цикл падений под «AppExit Default Restart». Вторая: неуспешный /health
# роняет Invoke-WebRequest в исключение, а мы печатали жёлтую строку и выходили
# НУЛЁМ — то есть накат всегда заканчивался «успешно», что бы ни случилось.
#
# 150 секунд — чуть больше самого короткого срока протухания: к этому моменту
# новый воркер обязан был отметиться хотя бы раз, и зелёный ответ уже про него,
# а не про прошлую жизнь.
$deadline = (Get-Date).AddSeconds(150)
$ok = $false
$last = ""
Write-Host "[*] Ждём http://127.0.0.1:8000/health (до 150 с)..." -ForegroundColor Cyan
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 15
        $last = $r.Content
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        $last = $_.Exception.Message
        if ($_.Exception.Response) {
            try {
                $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $last = $sr.ReadToEnd()
            } catch { }
        }
    }
    Start-Sleep -Seconds 5
}

if (-not $ok) {
    Write-Host "[X] /health не отдал 200 за 150 секунд — обновление НЕ подтверждено." -ForegroundColor Red
    Write-Host "    Последний ответ:" -ForegroundColor Red
    Write-Host "    $last" -ForegroundColor Red
    Write-Host "    Смотрите logs\web.err.log и logs\worker.err.log (-Encoding UTF8)." -ForegroundColor Red
    exit 1
}

Write-Host "[OK] health HTTP 200" -ForegroundColor Green
Write-Host $last
Write-Host "[OK] Обновление завершено." -ForegroundColor Green
