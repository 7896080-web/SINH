<#
.SYNOPSIS
    Развёртывание приложения sync_admin на Windows Server (локальный вариант,
    обмен с 1С через папку C:\sync, без FTP).

.DESCRIPTION
    Выполняет автоматизируемые шаги:
      1. Проверяет Python 3.11+.
      2. Создаёт venv и ставит зависимости.
      3. Создаёт каталоги обмена C:\sync\{tasks,results,archive}.
      4. Генерирует .env с секретами (если .env ещё нет) — SESSION_SECRET,
         SECRETS_ENCRYPTION_KEY (Fernet), DATABASE_URL, SYNC_DIR_*.
      5. Настраивает БД: PostgreSQL (если найден psql) либо SQLite (fallback).
      6. Прогоняет миграции Alembic.
      7. Создаёт первого пользователя админки (если пользователей ещё нет).
      8. Регистрирует Windows-службы веба и планировщика через NSSM
         (если nssm.exe доступен) — иначе печатает инструкцию.

    Идемпотентен: существующий .env не перезаписывается, пользователь-админ
    повторно не создаётся, службы переустанавливаются аккуратно.

.NOTES
    Запускать в PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА, из любой папки:
        powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
#>

[CmdletBinding()]
param(
    [string]$SyncRoot = "C:\sync",
    [int]$WebPort = 8000
)

$ErrorActionPreference = "Stop"

function Info($m)  { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

# --- 0. Пути ---
$ProjectRoot = Split-Path $PSScriptRoot -Parent
Info "Корень проекта: $ProjectRoot"
Set-Location $ProjectRoot

if (-not (Test-Path (Join-Path $ProjectRoot "requirements.txt"))) {
    Fail "Не найден requirements.txt — скрипт должен лежать в deploy\ внутри проекта."
}

# --- 1. Python ---
$pythonCmd = $null
foreach ($c in @("python", "py")) {
    try {
        $v = & $c --version 2>&1
        if ($v -match "Python 3\.(1[1-9]|[2-9]\d)") { $pythonCmd = $c; break }
    } catch { }
}
if (-not $pythonCmd) {
    Fail "Не найден Python 3.11+. Установите с https://www.python.org/downloads/windows/ (галочка 'Add to PATH'), затем перезапустите скрипт."
}
Ok "Python: $(& $pythonCmd --version)"

# --- 2. venv + зависимости ---
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Info "Создаю виртуальное окружение .venv ..."
    & $pythonCmd -m venv (Join-Path $ProjectRoot ".venv")
}
Info "Устанавливаю зависимости (pip install -r requirements.txt) ..."
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
Ok "Зависимости установлены."

# --- 3. Каталоги обмена ---
$dirTasks   = Join-Path $SyncRoot "tasks"
$dirResults = Join-Path $SyncRoot "results"
$dirArchive = Join-Path $SyncRoot "archive"
foreach ($d in @($dirTasks, $dirResults, $dirArchive)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}
Ok "Каталоги обмена: $SyncRoot\{tasks,results,archive}"

# --- 4. .env ---
$envPath = Join-Path $ProjectRoot ".env"
if (Test-Path $envPath) {
    Warn ".env уже существует — не трогаю (секреты и настройки сохраняются)."
} else {
    Info "Генерирую .env ..."

    # SESSION_SECRET через .NET RNG (48 байт base64)
    $bytes = New-Object 'System.Byte[]' 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $sessionSecret = [Convert]::ToBase64String($bytes)

    # SECRETS_ENCRYPTION_KEY через Fernet (cryptography уже установлен в venv)
    $fernetKey = (& $venvPython -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").Trim()

    # БД: PostgreSQL если есть psql, иначе SQLite
    $psql = Get-Command psql -ErrorAction SilentlyContinue
    if ($psql) {
        $dbPass = Read-Host "Пароль для пользователя БД sync_user (PostgreSQL)"
        $databaseUrl = "postgresql://sync_user:$dbPass@localhost:5432/sync_admin"
        $script:PgPassword = $dbPass
    } else {
        Warn "psql не найден — использую SQLite (для одного сервера с небольшим объёмом это допустимо; PostgreSQL надёжнее при одновременной работе веба и планировщика)."
        $sqlitePath = (Join-Path $ProjectRoot "sync_admin.db") -replace '\\','/'
        $databaseUrl = "sqlite:///$sqlitePath"
    }

    $envLines = @(
        "DATABASE_URL=$databaseUrl",
        "SESSION_SECRET=$sessionSecret",
        "SECRETS_ENCRYPTION_KEY=$fernetKey",
        "",
        "# Локальный HTTP (за обратным прокси с HTTPS — поставить 1)",
        "SESSION_COOKIE_SECURE=0",
        "",
        "# Склады на площадках (ID склада продавца на маркетплейсе)",
        "WAREHOUSE_ID_WB=",
        "WAREHOUSE_ID_OZON=",
        "WAREHOUSE_ID_KIT=",
        "",
        "# Канал обмена с 1С — локальная папка (совпадает с ПараметрыОбмена() в .epf)",
        "SYNC_DIR_TASKS=$dirTasks",
        "SYNC_DIR_RESULTS=$dirResults",
        "SYNC_DIR_ARCHIVE=$dirArchive"
    )
    # Пишем БЕЗ BOM (Set-Content -Encoding utf8 в PS5.1 добавляет BOM, а
    # python-dotenv тогда не видит первую переменную).
    [System.IO.File]::WriteAllLines($envPath, $envLines, (New-Object System.Text.UTF8Encoding($false)))
    Ok ".env создан (секреты сгенерированы, в чат не выводятся)."
}

# --- 5. PostgreSQL: создать роль и БД (если psql есть и БД postgres) ---
$psql = Get-Command psql -ErrorAction SilentlyContinue
if ($psql -and $script:PgPassword) {
    Info "Создаю роль sync_user и базу sync_admin (если ещё нет) ..."
    $env:PGPASSWORD = Read-Host "Пароль суперпользователя postgres (для создания роли/БД; Enter — пропустить)"
    if ($env:PGPASSWORD) {
        try {
            & psql -U postgres -h localhost -c "DO `$`$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='sync_user') THEN CREATE ROLE sync_user LOGIN PASSWORD '$($script:PgPassword)'; END IF; END `$`$;"
            $exists = (& psql -U postgres -h localhost -tAc "SELECT 1 FROM pg_database WHERE datname='sync_admin'")
            if ($exists -ne "1") {
                & psql -U postgres -h localhost -c "CREATE DATABASE sync_admin OWNER sync_user;"
            }
            Ok "PostgreSQL: роль и база готовы."
        } catch {
            Warn "Не удалось создать роль/БД автоматически ($_). Создайте вручную: CREATE ROLE sync_user LOGIN PASSWORD '...'; CREATE DATABASE sync_admin OWNER sync_user;"
        } finally {
            Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        }
    } else {
        Warn "Пароль postgres не задан — роль/БД не создаю. Убедитесь, что sync_user и база sync_admin уже существуют."
    }
}

# --- 6. Миграции ---
Info "Применяю миграции (alembic upgrade head) ..."
& $venvPython -m alembic upgrade head
Ok "Схема БД актуальна."

# --- 7. Первый пользователь ---
$userCount = & $venvPython -c "from app.database import SessionLocal; from app.models import User; s=SessionLocal(); print(s.query(User).count()); s.close()"
if ($userCount.Trim() -eq "0") {
    $adminPass = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 16 | ForEach-Object {[char]$_})
    & $venvPython (Join-Path $ProjectRoot "create_admin_user.py") admin $adminPass
    Ok "Создан пользователь админки: admin / $adminPass"
    Warn "Сохраните пароль — он показывается один раз."
} else {
    Warn "Пользователи уже есть ($($userCount.Trim())) — админа не создаю. Сброс пароля: create_admin_user.py или отдельный скрипт."
}

# --- 8. Службы Windows (NSSM) ---
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
$logDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

if ($nssm) {
    Info "Регистрирую службы через NSSM ..."

    function Set-NssmService($name, $params) {
        # Проверяем существование службы средствами PowerShell (не `nssm status`,
        # который на несуществующей службе пишет в stderr и валит скрипт под Stop).
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
            & nssm remove $name confirm | Out-Null
        }
        & nssm install $name $venvPython | Out-Null
        & nssm set $name AppParameters $params | Out-Null
        & nssm set $name AppDirectory $ProjectRoot | Out-Null
        & nssm set $name AppStdout (Join-Path $logDir "$name.log") | Out-Null
        & nssm set $name AppStderr (Join-Path $logDir "$name.err.log") | Out-Null
        & nssm set $name Start SERVICE_AUTO_START | Out-Null
        & nssm set $name AppExit Default Restart | Out-Null
        # Ротация логов. Без неё worker.err.log растёт, пока есть диск, и
        # однажды его нельзя ни открыть, ни найти в нём строку.
        & nssm set $name AppRotateFiles 1 | Out-Null
        & nssm set $name AppRotateOnline 1 | Out-Null
        & nssm set $name AppRotateBytes 10485760 | Out-Null
    }

    Set-NssmService "sync_admin_web"    "-m uvicorn app.main:app --host 127.0.0.1 --port $WebPort"
    Set-NssmService "sync_admin_worker" "-m app.workers.scheduler"

    & nssm start sync_admin_web
    & nssm start sync_admin_worker
    Ok "Службы sync_admin_web (порт $WebPort) и sync_admin_worker запущены."
    Info "Проверка: http://127.0.0.1:$WebPort/login  и  http://127.0.0.1:$WebPort/health"
} else {
    Warn "NSSM не найден — службы не зарегистрированы."
    Write-Host ""
    Write-Host "Чтобы запускать веб и планировщик как службы, поставьте NSSM (https://nssm.cc/download,"
    Write-Host "распакуйте nssm.exe в папку из PATH) и выполните повторно, ИЛИ вручную:"
    Write-Host ""
    Write-Host "  nssm install sync_admin_web `"$venvPython`" -m uvicorn app.main:app --host 127.0.0.1 --port $WebPort"
    Write-Host "  nssm set sync_admin_web AppDirectory `"$ProjectRoot`""
    Write-Host "  nssm install sync_admin_worker `"$venvPython`" -m app.workers.scheduler"
    Write-Host "  nssm set sync_admin_worker AppDirectory `"$ProjectRoot`""
    Write-Host ""
    Write-Host "Для разовой проверки без служб:"
    Write-Host "  $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $WebPort"
    Write-Host "  $venvPython -m app.workers.scheduler   (в отдельном окне)"
}

Write-Host ""
Ok "Готово. Веб: http://127.0.0.1:$WebPort  |  папка обмена: $SyncRoot  |  логи: $logDir"
