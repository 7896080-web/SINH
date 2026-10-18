<#
.SYNOPSIS
    Сервер (Windows, рядом с 1С и sync_admin): первая установка программы
    «Финансы площадок» службой Windows.

.DESCRIPTION
    Файлы уже разложены в C:\mpfinance пакетом (mpfinance_finN.ps1).

    1. Проверяет: права администратора, nssm, файлы программы на месте, папки
       обмена 1С есть, порт свободен, это не каталог sync_admin.
    2. Python 3.11+ (тот же, что у sync_admin), СВОЙ venv и зависимости.
    3. .env: если его нет — создаёт со СВОИМИ ключами (шифрование ключей площадок,
       сессии), путём к базе, папками обмена C:\sync\tasks -> results\finance и
       путём к базе sync_admin (только чтение). Существующий .env не трогает.
    4. Миграции и тесты; первый пользователь.
    5. Служба «mpfinance» через NSSM (как службы sync_admin и «Репрайсера»):
       работает без входа в систему, перезапускается при сбое, журналы с
       ротацией. Слушает только 127.0.0.1:8003 — снаружи сервера её не видно.
    6. Ярлык «Финансы площадок» на общем рабочем столе — через Chrome/Edge.

    sync_admin, «Репрайсер», 1С и обмен других программ не трогает. Повторный
    запуск безопасен.

    PowerShell ОТ АДМИНИСТРАТОРА:
        powershell -ExecutionPolicy Bypass -File C:\mpfinance\deploy\install_server.ps1
#>
[CmdletBinding()]
param(
    [int]$WebPort = 8003,
    [string]$Service = "mpfinance",
    [string]$SyncRoot = "C:\sync",
    [string]$SyncAdminDb = "C:\sync_admin\sync_admin.db"
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Запустите PowerShell от администратора." }
if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) { Fail "Нет nssm в PATH (им поставлены службы sync_admin) — найдите nssm.exe и добавьте его папку в PATH." }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
if ($Root -like "*sync_admin*") { Fail "$Root — каталог sync_admin. Программа ставится в свой каталог, C:\mpfinance." }
if (-not (Test-Path (Join-Path $Root "finapp\main.py"))) { Fail "В $Root нет finapp\main.py — сначала запустите пакет mpfinance_finN.ps1." }
Info "Каталог программы: $Root"

# --- 1. Папки обмена 1С ---
if (-not (Test-Path "$SyncRoot\tasks")) { Fail "Нет $SyncRoot\tasks — это не сервер с 1С и sync_admin, или обмен лежит в другом месте (-SyncRoot)." }
foreach ($d in @("$SyncRoot\results\finance", "$SyncRoot\archive\finance")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Ok "Обмен с 1С: задания $SyncRoot\tasks (task_fin_*), ответы $SyncRoot\results\finance"
if (-not (Test-Path $SyncAdminDb)) { Warn "Нет $SyncAdminDb — мэппинг и сверка с заказами Sync Admin будут недоступны (путь — MPFIN_SYNC_DB в .env)." }

# --- 2. Порт ---
$svcExists = [bool](Get-Service -Name $Service -ErrorAction SilentlyContinue)
if (-not $svcExists) {
    $busy = Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Fail "Порт $WebPort уже занят (процесс $($busy[0].OwningProcess)). Укажите другой: -WebPort 8004" }
}

# --- 3. Python, venv ---
$py = $null
foreach ($c in @("python", "py")) {
    try {
        $v = & $c --version 2>&1
        if ($v -match "Python 3\.(1[1-9]|[2-9]\d)") { $py = (Get-Command $c).Source; break }
    } catch { }
}
if (-not $py) { Fail "Не найден Python 3.11+ (тот, что стоит для sync_admin, должен подойти — проверьте PATH)." }
Ok "Python: $py ($(& $py --version))"
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

# --- 4. .env ---
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    Ok ".env уже есть — не трогаю"
} else {
    $secretsKey = (& $venvPy -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())").Trim()
    $bytes = New-Object 'System.Byte[]' 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $sessionSecret = [Convert]::ToBase64String($bytes)
    $lines = @(
        # В скобках обязательно: запятая в PowerShell связывает раньше «+».
        ("DATABASE_URL=sqlite:///" + ($Root -replace "\\", "/") + "/mpfinance.db"),
        "MPFIN_SECRETS_KEY=$secretsKey",
        "MPFIN_SESSION_SECRET=$sessionSecret",
        "MPFIN_ONEC_TASKS_DIR=$SyncRoot\tasks",
        "MPFIN_ONEC_RESULTS_DIR=$SyncRoot\results\finance",
        "MPFIN_ONEC_ARCHIVE_DIR=$SyncRoot\archive\finance",
        "MPFIN_SYNC_DB=$SyncAdminDb",
        "MPFIN_BACKUP_DIR=$Root\backups"
    )
    Set-Content -Path $envFile -Value $lines -Encoding UTF8
    Ok ".env создан. MPFIN_SECRETS_KEY — ключ шифрования ключей площадок: сохраните его отдельно от копий базы."
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root "backups"), (Join-Path $Root "logs") | Out-Null

# --- 5. Миграции, тесты, пользователь ---
if ($svcExists) {
    Info "Останавливаю службу $Service"
    & nssm stop $Service | Out-Null
}
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
Ok "Тесты зелёные"
$count = & $venvPy -c "import finapp; from finapp.database import SessionLocal; from finapp.models import User; s=SessionLocal(); print(s.query(User).count())"
if ($count -eq "0") {
    $login = Read-Host "Логин первого пользователя"
    $pass = Read-Host "Пароль (не короче 8 символов)"
    & $venvPy (Join-Path $Root "create_user.py") $login $pass
} else {
    Ok "Пользователей в базе: $count"
}

# --- 6. Служба ---
$logDir = Join-Path $Root "logs"
if ($svcExists) { & nssm remove $Service confirm | Out-Null }
& nssm install $Service $venvPy | Out-Null
& nssm set $Service AppParameters "-m uvicorn finapp.main:app --host 127.0.0.1 --port $WebPort" | Out-Null
& nssm set $Service AppDirectory $Root | Out-Null
& nssm set $Service DisplayName "Финансы площадок (отчёты, юнит-экономика)" | Out-Null
& nssm set $Service AppStdout (Join-Path $logDir "mpfinance.log") | Out-Null
& nssm set $Service AppStderr (Join-Path $logDir "mpfinance.err.log") | Out-Null
& nssm set $Service Start SERVICE_AUTO_START | Out-Null
& nssm set $Service AppExit Default Restart | Out-Null
& nssm set $Service AppRotateFiles 1 | Out-Null
& nssm set $Service AppRotateOnline 1 | Out-Null
& nssm set $Service AppRotateBytes 10485760 | Out-Null
& nssm start $Service | Out-Null
$url = "http://127.0.0.1:$WebPort"
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    try { Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch { Start-Sleep -Seconds 1 }
}
if (-not $up) { Fail "Служба $Service не отвечает на $url — журнал: $logDir\mpfinance.err.log (открывать с -Encoding UTF8)" }
Ok "Служба $Service работает: $url"

# --- 7. Ярлык ---
$browser = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Yandex\YandexBrowser\Application\browser.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$desk = Join-Path $env:PUBLIC "Desktop"
if (-not (Test-Path $desk)) { $desk = [Environment]::GetFolderPath("Desktop") }
if ($browser) {
    $lnk = Join-Path $desk "Финансы площадок.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $browser
    $s.Arguments = '"' + $url + '"'
    $s.WorkingDirectory = Split-Path $browser
    $s.Description = "Финансы площадок — $url"
    $s.Save()
    Ok "Ярлык «Финансы площадок» на рабочем столе ($desk)"
} else {
    Warn "Chrome/Edge не найден — ярлык не создан. Откройте $url в Chrome вручную (Internet Explorer не подойдёт)."
}

Write-Host ""
Ok "Готово. Дальше: войти -> «Диагностика» -> «Проверить обработку 1С» (нужна обработка mark-4) -> «API-ключи»: кабинеты, токены, почта Lamoda."
