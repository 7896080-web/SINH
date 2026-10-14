<#
.SYNOPSIS
    Сервер (Windows, рядом с 1С и sync_admin): установка «Репрайсера» службой Windows.

.DESCRIPTION
    Перенос с офисного компьютера: там deploy\export_for_server.ps1 собрал архив
    (код, копия базы, .env, история эталона), здесь он распакован в C:\repricer.

    1. Проверяет: права администратора, файлы программы, .env и база на месте,
       папки обмена 1С есть, порт свободен, это не каталог sync_admin.
    2. Python 3.11+ (тот же, что у sync_admin), СВОЙ venv и зависимости.
    3. Переписывает в .env то, что зависело от компьютера: путь к базе и копиям,
       обмен с 1С — локальные папки C:\sync вместо SFTP (1С здесь же). Прежний .env
       остаётся рядом как .env.pc.
    4. Миграции и тесты.
    5. Служба «repricer» через NSSM (как службы sync_admin): работает без входа в
       систему, перезапускается при сбое, журналы с ротацией. Слушает только
       127.0.0.1:8002 — снаружи сервера программы не видно.
    6. Ярлык «Репрайсер» на общем рабочем столе — через Chrome/Edge, не через
       Internet Explorer (на Windows Server он браузер по умолчанию, и страницы
       программы на нём не работают).

    sync_admin, 1С и обмен других программ не трогает. Повторный запуск безопасен.

    PowerShell ОТ АДМИНИСТРАТОРА:
        powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\install_server.ps1
#>
[CmdletBinding()]
param(
    [int]$WebPort = 8002,
    [string]$Service = "repricer",
    [string]$SyncRoot = "C:\sync"
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
if ($Root -like "*sync_admin*") { Fail "$Root — каталог sync_admin. Репрайсер ставится в свой каталог, C:\repricer." }
if (-not (Test-Path (Join-Path $Root "priceapp\main.py"))) { Fail "В $Root нет priceapp\main.py — распакуйте архив переноса в $Root." }
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { Fail "Нет $envFile — он приходит в архиве переноса (export_for_server.ps1 на офисном компьютере)." }
if (-not (Test-Path (Join-Path $Root "repricer.db"))) { Warn "Нет repricer.db — программа начнёт с пустой базы. Если это перенос — распакуйте архив целиком." }
if (Test-Path (Join-Path $Root "MOVED_TO_SERVER.txt")) { Remove-Item (Join-Path $Root "MOVED_TO_SERVER.txt") -Force }
Info "Каталог программы: $Root"

# --- 1. Папки обмена 1С ---
if (-not (Test-Path "$SyncRoot\tasks")) { Fail "Нет $SyncRoot\tasks — это не сервер с 1С и sync_admin, или обмен лежит в другом месте (-SyncRoot)." }
foreach ($d in @("$SyncRoot\results\pricing", "$SyncRoot\archive\pricing")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Ok "Обмен с 1С: $SyncRoot\tasks, ответы $SyncRoot\results\pricing"

# --- 2. Порт ---
$svcExists = [bool](Get-Service -Name $Service -ErrorAction SilentlyContinue)
if (-not $svcExists) {
    $busy = Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Fail "Порт $WebPort уже занят (процесс $($busy[0].OwningProcess)). Укажите другой: -WebPort 8003" }
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

# --- 4. .env под сервер ---
$pcCopy = Join-Path $Root ".env.pc"
if (-not (Test-Path $pcCopy)) { Copy-Item $envFile $pcCopy }
$drop = '^(DATABASE_URL|REPRICER_BACKUP_DIR|REPRICER_ETALON_DIR|REPRICER_ONEC_ARCHIVE_DIR|REPRICER_ONEC_RESULTS_DIR|REPRICER_ONEC_TASKS_DIR|REPRICER_ONEC_SFTP_[A-Z_]+)='
$lines = @(Get-Content $envFile -Encoding UTF8 | ForEach-Object { $_.TrimStart([char]0xFEFF) } | Where-Object { $_ -notmatch $drop })
if (-not ($lines | Where-Object { $_ -match '^REPRICER_SECRETS_KEY=.+' })) {
    Fail ".env без REPRICER_SECRETS_KEY — без него не прочитать ключи площадок. Возьмите .env с офисного компьютера."
}
$lines += @(
    "DATABASE_URL=sqlite:///" + ($Root -replace "\\", "/") + "/repricer.db",
    "REPRICER_BACKUP_DIR=$Root\backups",
    "REPRICER_ETALON_DIR=$Root\etalon_history",
    "REPRICER_ONEC_TASKS_DIR=$SyncRoot\tasks",
    "REPRICER_ONEC_RESULTS_DIR=$SyncRoot\results\pricing",
    "REPRICER_ONEC_ARCHIVE_DIR=$SyncRoot\archive\pricing"
)
Set-Content -Path $envFile -Value $lines -Encoding UTF8
New-Item -ItemType Directory -Force -Path (Join-Path $Root "backups"), (Join-Path $Root "logs"), (Join-Path $Root "etalon_history") | Out-Null
Ok ".env: база $Root\repricer.db, обмен с 1С — локальные папки $SyncRoot (без SFTP); прежний — .env.pc"

# --- 5. Миграции, тесты ---
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
$count = & $venvPy -c "import priceapp; from priceapp.database import SessionLocal; from priceapp.models import User; s=SessionLocal(); print(s.query(User).count())"
if ($count -eq "0") {
    $login = Read-Host "Логин первого пользователя"
    $pass = Read-Host "Пароль (не короче 8 символов)"
    & $venvPy (Join-Path $Root "create_user.py") $login $pass
} else {
    Ok "Пользователи перенесены ($count) — входите прежним логином"
}

# --- 6. Служба ---
$logDir = Join-Path $Root "logs"
if ($svcExists) { & nssm remove $Service confirm | Out-Null }
& nssm install $Service $venvPy | Out-Null
& nssm set $Service AppParameters "-m uvicorn priceapp.main:app --host 127.0.0.1 --port $WebPort" | Out-Null
& nssm set $Service AppDirectory $Root | Out-Null
& nssm set $Service DisplayName "Репрайсер (цены на площадках)" | Out-Null
& nssm set $Service AppStdout (Join-Path $logDir "repricer.log") | Out-Null
& nssm set $Service AppStderr (Join-Path $logDir "repricer.err.log") | Out-Null
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
if (-not $up) { Fail "Служба $Service не отвечает на $url — журнал: $logDir\repricer.err.log (открывать с -Encoding UTF8)" }
Ok "Служба $Service работает: $url"

# --- 7. Ярлык ---
$browser = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Yandex\YandexBrowser\Application\browser.exe",
    "$env:ProgramFiles\Mozilla Firefox\firefox.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$desk = Join-Path $env:PUBLIC "Desktop"
if (-not (Test-Path $desk)) { $desk = [Environment]::GetFolderPath("Desktop") }
$icon = Join-Path $Root "deploy\repricer.ico"
$shell = New-Object -ComObject WScript.Shell
if ($browser) {
    $lnk = Join-Path $desk "Репрайсер.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $browser
    $s.Arguments = '"' + $url + '"'
    $s.WorkingDirectory = Split-Path $browser
    if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
    $s.Description = "Репрайсер — $url"
    $s.Save()
    Ok "Ярлык «Репрайсер» на рабочем столе ($desk), браузер $browser"
} else {
    Warn "Chrome/Edge не найден — ярлык не создан. Откройте $url в Chrome вручную (Internet Explorer не подойдёт)."
}

Write-Host ""
Ok "Готово. Дальше: войти прежним логином -> «Диагностика»: обмен с 1С — «локальные папки» -> «Запросить себестоимость»."
Warn "Удалите архив переноса (C:\temp\repricer_move.zip) — в нём ключи площадок."
