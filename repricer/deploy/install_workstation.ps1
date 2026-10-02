<#
.SYNOPSIS
    Установка программы «Репрайсер» на офисный компьютер.

.DESCRIPTION
    Программа работает на этом компьютере, а обмен с 1С идёт по SFTP до сервера
    (порт 443; на сервере — OpenSSH Server, deploy\SFTP_1C.md). Служб нет: программа
    запускается ярлыком «Репрайсер» и работает, пока открыта.

    1. Находит Python 3.11+.
    2. Создаёт C:\repricer\.venv и ставит зависимости.
    3. Создаёт .env со СВОИМИ секретами и адресом сервера (если .env нет).
    4. Создаёт СВОЙ ключ SFTP (ssh\id_ed25519) и печатает открытую часть: её
       дописывают на сервере в authorized_keys той же учётной записи, что у
       «Маркировки» (marking/deploy/SFTP_1C.md, шаг 4) — ещё одной строкой.
    5. Записывает ключ сервера в ssh\known_hosts и печатает его отпечаток —
       сверить с сервером ОБЯЗАТЕЛЬНО. Если на этом компьютере уже стоит
       «Маркировка» (C:\marking\ssh\known_hosts), берётся её проверенный файл.
    6. Прогоняет миграции и тесты, заводит первого пользователя.
    7. Кладёт ярлык «Репрайсер» на рабочий стол (и в автозапуск с -Autostart).

    Идемпотентен: существующие .env, ключ и known_hosts не трогает.

.NOTES
    Файлы программы должны уже лежать в C:\repricer (содержимое каталога repricer/
    репозитория). Права администратора НЕ нужны:
        powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\install_workstation.ps1 -Server 136.243.92.95
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [int]$SshPort = 443,
    [string]$SftpUser = "marking_sftp",
    [int]$WebPort = 8002,
    [switch]$Autostart
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
if (-not (Test-Path (Join-Path $Root "priceapp\main.py"))) {
    Fail "В $Root нет priceapp\main.py — скопируйте сюда содержимое каталога repricer/ репозитория."
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
if (-not $py) { Fail "Не найден Python 3.11+. Поставьте с python.org (галочка «Add python.exe to PATH») и запустите снова." }
Ok "Python: $py ($(& $py --version))"

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
$sshDir = Join-Path $Root "ssh"
New-Item -ItemType Directory -Force -Path $sshDir, (Join-Path $Root "backups"), (Join-Path $Root "logs"), (Join-Path $Root "onec_archive") | Out-Null
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    $key = & $venvPy -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    $secret = & $venvPy -c "import secrets; print(secrets.token_urlsafe(48))"
    $db = ($Root -replace "\\", "/") + "/repricer.db"
    $lines = @(
        "DATABASE_URL=sqlite:///$db",
        "REPRICER_SECRETS_KEY=$key",
        "REPRICER_SESSION_SECRET=$secret",
        "REPRICER_ONEC_SFTP_HOST=$Server",
        "REPRICER_ONEC_SFTP_PORT=$SshPort",
        "REPRICER_ONEC_SFTP_USER=$SftpUser",
        "REPRICER_ONEC_SFTP_KEY=$sshDir\id_ed25519",
        "REPRICER_ONEC_SFTP_KNOWN_HOSTS=$sshDir\known_hosts",
        "REPRICER_ONEC_ARCHIVE_DIR=$Root\onec_archive",
        "REPRICER_BACKUP_DIR=$Root\backups"
    )
    Set-Content -Path $envFile -Value $lines -Encoding UTF8
    Ok ".env создан"
    Warn "СОХРАНИТЕ REPRICER_SECRETS_KEY из $envFile туда, где храните пароли (НЕ на Яндекс.Диск): без него копия базы бесполезна."
} else {
    Ok ".env уже есть — не трогаю"
}

# --- 4. Ключ SFTP ---
$keyFile = Join-Path $sshDir "id_ed25519"
if (-not (Test-Path $keyFile)) {
    if (-not (Get-Command ssh-keygen -ErrorAction SilentlyContinue)) {
        Fail "Нет ssh-keygen: «Параметры» -> «Приложения» -> «Дополнительные компоненты» -> «Клиент OpenSSH»."
    }
    # Пустая парольная фраза: обмен идёт без человека. Ключ умеет только SFTP
    # в C:\sync сервера — сервер запирает эту учётную запись (SFTP_1C.md).
    & ssh-keygen -q -t ed25519 -N '""' -C "repricer@$env:COMPUTERNAME" -f $keyFile
    if ($LASTEXITCODE -ne 0) { Fail "ключ не создан" }
    Ok "Ключ SFTP создан"
}
# Закрытый ключ — только владельцу. C:\repricer наследует права корня диска
# («Пользователи» читают), и клиент OpenSSH отвергает такой ключ как «too open» —
# ручная проверка sftp не прошла бы. Да и читать его другим незачем.
& icacls $keyFile /inheritance:r /grant:r "$($env:USERNAME):F" /grant:r "*S-1-5-18:F" | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "не удалось ограничить права на $keyFile — проверьте вручную" }
Write-Host ""
Write-Host "Открытый ключ — одна строка. На сервере её ДОПИСЫВАЮТ в authorized_keys учётной записи $SftpUser (как у «Маркировки», marking\deploy\SFTP_1C.md, шаг 4):" -ForegroundColor Cyan
Get-Content "$keyFile.pub"
Write-Host ""

# --- 5. Ключ сервера ---
$known = Join-Path $sshDir "known_hosts"
$markingKnown = "C:\marking\ssh\known_hosts"
if (-not (Test-Path $known) -and (Test-Path $markingKnown)) {
    # Сервер тот же, что у «Маркировки», и его ключ там уже сверен человеком.
    Copy-Item $markingKnown $known
    Ok "known_hosts взят у «Маркировки» (сервер тот же, ключ уже сверен)"
}
if (-not (Test-Path $known)) {
    Info "Спрашиваю ключ сервера $Server`:$SshPort"
    $scan = & ssh-keyscan -p $SshPort -t ed25519 $Server 2>$null
    if (-not $scan) {
        Warn "Сервер не ответил на $SshPort — known_hosts не создан. Запустите установку снова, когда SSH на сервере будет готов."
    } else {
        Set-Content -Path $known -Value $scan -Encoding ascii
        Write-Host "Отпечаток ключа сервера — СВЕРЬТЕ с тем, что показал сервер (marking\deploy\SFTP_1C.md, шаг 5):" -ForegroundColor Yellow
        & ssh-keygen -l -f $known
        Warn "Не совпало — удалите $known и НЕ продолжайте: вы говорите не с тем сервером."
    }
} else {
    Ok "known_hosts уже есть — не трогаю"
}

# --- 6. Миграции, тесты, пользователь ---
Info "Миграции"
& $venvPy -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head завершился с ошибкой" }
Info "Тесты"
$saved = $env:DATABASE_URL
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
& $venvPy -m pytest -q
$rc = $LASTEXITCODE
if ($saved) { $env:DATABASE_URL = $saved }
if ($rc -ne 0) { Fail "Тесты красные — установку не заканчиваю" }
Ok "Тесты зелёные"
$count = & $venvPy -c "import priceapp; from priceapp.database import SessionLocal; from priceapp.models import User; s=SessionLocal(); print(s.query(User).count())"
if ($count -eq "0") {
    $login = Read-Host "Логин первого пользователя"
    $pass = Read-Host "Пароль (не короче 8 символов)"
    & $venvPy (Join-Path $Root "create_user.py") $login $pass
}

# --- 7. Ярлык ---
$shortcutArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "deploy\create_shortcut.ps1"), "-Port", $WebPort)
if ($Autostart) { $shortcutArgs += "-Autostart" }
& powershell @shortcutArgs
if ($LASTEXITCODE -ne 0) { Fail "Ярлык не создан" }
Ok "Ярлык «Репрайсер» на рабочем столе$(if ($Autostart) { ' и в автозагрузке' })"
Ok "Готово. Дальше: ключ на сервер, обработка 1С mark-3, затем в программе «API-ключи», «Курс $» и «Диагностика» -> «Запросить себестоимость»."
