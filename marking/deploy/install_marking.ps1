<#
.SYNOPSIS
    Установка программы «Маркировка и поставки» на Windows Server рядом с
    sync_admin (C:\marking, служба на 127.0.0.1:8001).

.DESCRIPTION
    1. Находит Python 3.11+ (PATH, иначе — тот, на котором построен venv sync_admin).
    2. Создаёт C:\marking\.venv и ставит зависимости.
    3. Создаёт .env с СОБСТВЕННЫМИ секретами (не ключи sync_admin), если его нет.
    4. Создаёт папки обмена маркировки: C:\sync\results\marking, C:\sync\archive\marking.
    5. Прогоняет миграции и тесты.
    6. Заводит первого пользователя (если пользователей нет).
    7. Регистрирует службы marking_web и marking_worker через NSSM.

    Идемпотентен: существующий .env не трогает, службы переустанавливает.
    sync_admin не трогает НИЧЕМ: ни файлов, ни служб, ни базы.

.NOTES
    Файлы программы должны уже лежать в C:\marking (содержимое каталога marking/
    репозитория). Запуск — PowerShell от администратора:
        powershell -ExecutionPolicy Bypass -File C:\marking\deploy\install_marking.ps1
#>
[CmdletBinding()]
param(
    [int]$WebPort = 8001,
    [string]$SyncRoot = "C:\sync"
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
if (-not (Test-Path (Join-Path $Root "markapp\main.py"))) {
    Fail "В $Root нет markapp\main.py — скопируйте сюда содержимое каталога marking/ репозитория."
}
Info "Каталог программы: $Root"

# --- 1. Python ---
$py = $null
foreach ($c in @("python", "py")) {
    try {
        $v = & $c --version 2>&1
        if ($v -match "Python 3\.(1[1-9]|[2-9]\d)") { $py = (Get-Command $c).Source; break }
    } catch { }
}
if (-not $py) {
    # Тот же Python, на котором работает sync_admin (его venv знает, где база).
    $cfg = "C:\sync_admin\.venv\pyvenv.cfg"
    if (Test-Path $cfg) {
        $home_ = (Get-Content $cfg | Where-Object { $_ -match "^home\s*=" }) -replace "^home\s*=\s*", ""
        $cand = Join-Path $home_.Trim() "python.exe"
        if (Test-Path $cand) { $py = $cand }
    }
}
if (-not $py) { Fail "Не найден Python 3.11+." }
Ok "Python: $py ($(& $py --version))"
if ($py -like "*\Users\*") {
    Warn "Python стоит в профиле пользователя. Удаление профиля или обновление Python остановит обе программы (sync_admin и маркировку)."
}

# --- 2. venv и зависимости ---
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Info "Создаю venv"
    & $py -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "venv не создан" }
}
Info "Ставлю зависимости"
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой" }
Ok "Зависимости установлены"

# --- 3. .env ---
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    $key = & $venvPy -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    $secret = & $venvPy -c "import secrets; print(secrets.token_urlsafe(48))"
    $db = ($Root -replace "\\", "/") + "/marking.db"
    $lines = @(
        "DATABASE_URL=sqlite:///$db",
        "MARKING_SECRETS_KEY=$key",
        "MARKING_SESSION_SECRET=$secret",
        "MARKING_ONEC_TASKS_DIR=$SyncRoot\tasks",
        "MARKING_ONEC_RESULTS_DIR=$SyncRoot\results\marking",
        "MARKING_ONEC_ARCHIVE_DIR=$SyncRoot\archive\marking",
        "MARKING_BACKUP_DIR=$Root\backups",
        "MARKING_RCLONE_EXE=$Root\tools\rclone.exe",
        "MARKING_RCLONE_CONFIG=$Root\rclone.conf",
        "MARKING_RCLONE_REMOTE=",
        # Здесь фоновую работу делает служба marking_worker, веб её не дублирует.
        "MARKING_BACKGROUND=0"
    )
    Set-Content -Path $envFile -Value $lines -Encoding UTF8
    Ok ".env создан"
    Warn "СОХРАНИТЕ MARKING_SECRETS_KEY из $envFile туда, где храните пароли (НЕ на Яндекс.Диск): без него копия базы бесполезна."
} else {
    Ok ".env уже есть — не трогаю"
}

# --- 4. Папки ---
foreach ($d in @("$SyncRoot\tasks", "$SyncRoot\results\marking", "$SyncRoot\archive\marking",
                 (Join-Path $Root "backups"), (Join-Path $Root "logs"), (Join-Path $Root "tools"))) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Ok "Папки на месте"

# --- 5. Миграции и тесты ---
Info "Миграции"
& $venvPy -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head завершился с ошибкой" }
Info "Тесты"
$saved = $env:DATABASE_URL
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
& $venvPy -m pytest -q
$rc = $LASTEXITCODE
if ($saved) { $env:DATABASE_URL = $saved }
if ($rc -ne 0) { Fail "Тесты красные — службы не устанавливаю" }
Ok "Тесты зелёные"

# --- 6. Пользователь ---
$count = & $venvPy -c "import markapp; from markapp.database import SessionLocal; from markapp.models import User; s=SessionLocal(); print(s.query(User).count())"
if ($count -eq "0") {
    $login = Read-Host "Логин первого пользователя"
    $pass = Read-Host "Пароль (не короче 8 символов)"
    & $venvPy (Join-Path $Root "create_user.py") $login $pass
}

# --- 7. Службы ---
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) { Fail "nssm не найден в PATH (он же используется службами sync_admin)." }
$logDir = Join-Path $Root "logs"
function Set-Svc($name, $params) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        & nssm stop $name | Out-Null
        & nssm remove $name confirm | Out-Null
    }
    & nssm install $name $venvPy | Out-Null
    & nssm set $name AppParameters $params | Out-Null
    & nssm set $name AppDirectory $Root | Out-Null
    & nssm set $name AppStdout (Join-Path $logDir "$name.log") | Out-Null
    & nssm set $name AppStderr (Join-Path $logDir "$name.err.log") | Out-Null
    & nssm set $name Start SERVICE_AUTO_START | Out-Null
    & nssm set $name AppExit Default Restart | Out-Null
    & nssm set $name AppRotateFiles 1 | Out-Null
    & nssm set $name AppRotateOnline 1 | Out-Null
    & nssm set $name AppRotateBytes 10485760 | Out-Null
}
Set-Svc "marking_web"    "-m uvicorn markapp.main:app --host 127.0.0.1 --port $WebPort"
Set-Svc "marking_worker" "-m markapp.workers.scheduler"
& nssm start marking_worker | Out-Null
& nssm start marking_web | Out-Null

# /health: воркер должен отработать обмен с 1С хотя бы раз.
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 5
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$WebPort/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if (-not $ok) { Fail "/health не зелёный за 150 с — смотрите $logDir\marking_*.err.log" }
Ok "Готово: http://127.0.0.1:$WebPort  (логи: $logDir)"
Write-Host "Дальше: обновить обработку 1С по marking\1c\ОБНОВЛЕНИЕ_ОБРАБОТКИ.md и нажать «Отправить PING» на «Диагностике»."
