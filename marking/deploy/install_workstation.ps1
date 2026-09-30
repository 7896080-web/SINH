<#
.SYNOPSIS
    Установка программы «Маркировка и поставки» на рабочий компьютер с КриптоПро.

.DESCRIPTION
    Программа работает на этом компьютере, а обмен с 1С идёт по SFTP до сервера
    (порт 443; на сервере — OpenSSH Server, deploy\SFTP_1C.md). Служб нет: программа
    запускается ярлыком «Маркировка» и работает, пока открыта.

    1. Находит Python 3.11+.
    2. Создаёт C:\marking\.venv и ставит зависимости.
    3. Создаёт .env со СВОИМИ секретами и адресом сервера (если .env нет).
    4. Создаёт ключ SFTP (ssh\id_ed25519) и печатает открытую часть для сервера.
    5. Записывает ключ сервера в ssh\known_hosts (deploy\fetch_host_key.py) и печатает его отпечаток —
       сверить с сервером ОБЯЗАТЕЛЬНО (deploy\SFTP_1C.md, шаг 5).
    6. Прогоняет миграции и тесты, заводит первого пользователя.
    7. Кладёт ярлык «Маркировка» на рабочий стол (и в автозапуск с -Autostart).

    Идемпотентен: существующие .env, ключ и known_hosts не трогает.

.NOTES
    Файлы программы должны уже лежать в C:\marking (содержимое каталога marking/
    репозитория). Права администратора НЕ нужны:
        powershell -ExecutionPolicy Bypass -File C:\marking\deploy\install_workstation.ps1 -Server 136.243.92.95
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [int]$SshPort = 443,
    [string]$SftpUser = "marking_sftp",
    [int]$WebPort = 8001,
    [switch]$Autostart
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
New-Item -ItemType Directory -Force -Path $sshDir, (Join-Path $Root "backups"), (Join-Path $Root "logs"), (Join-Path $Root "tools"), (Join-Path $Root "onec_archive") | Out-Null
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    $key = & $venvPy -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    $secret = & $venvPy -c "import secrets; print(secrets.token_urlsafe(48))"
    $db = ($Root -replace "\\", "/") + "/marking.db"
    $lines = @(
        "DATABASE_URL=sqlite:///$db",
        "MARKING_SECRETS_KEY=$key",
        "MARKING_SESSION_SECRET=$secret",
        "MARKING_ONEC_SFTP_HOST=$Server",
        "MARKING_ONEC_SFTP_PORT=$SshPort",
        "MARKING_ONEC_SFTP_USER=$SftpUser",
        "MARKING_ONEC_SFTP_KEY=$sshDir\id_ed25519",
        "MARKING_ONEC_SFTP_KNOWN_HOSTS=$sshDir\known_hosts",
        "MARKING_ONEC_ARCHIVE_DIR=$Root\onec_archive",
        "MARKING_BACKUP_DIR=$Root\backups",
        "MARKING_RCLONE_EXE=$Root\tools\rclone.exe",
        "MARKING_RCLONE_CONFIG=$Root\rclone.conf",
        "MARKING_RCLONE_REMOTE="
    )
    Set-Content -Path $envFile -Value $lines -Encoding UTF8
    Ok ".env создан"
    Warn "СОХРАНИТЕ MARKING_SECRETS_KEY из $envFile туда, где храните пароли (НЕ на Яндекс.Диск): без него копия базы бесполезна."
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
    & ssh-keygen -q -t ed25519 -N '""' -C "marking@$env:COMPUTERNAME" -f $keyFile
    if ($LASTEXITCODE -ne 0) { Fail "ключ не создан" }
    Ok "Ключ SFTP создан"
}
# Закрытый ключ — только владельцу. C:\marking наследует права корня диска
# («Пользователи» читают), и клиент OpenSSH отвергает такой ключ как «too open» —
# ручная проверка sftp не прошла бы. Да и читать его другим незачем.
& icacls $keyFile /inheritance:r /grant:r "$($env:USERNAME):F" /grant:r "*S-1-5-18:F" | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "не удалось ограничить права на $keyFile — проверьте вручную" }
Write-Host ""
Write-Host "Открытый ключ — одна строка, её кладут на сервер (SFTP_1C.md, шаг 4):" -ForegroundColor Cyan
Get-Content "$keyFile.pub"
Write-Host ""

# --- 5. Ключ сервера ---
$known = Join-Path $sshDir "known_hosts"
if (-not (Test-Path $known)) {
    Info "Спрашиваю ключ сервера $Server`:$SshPort"
    # Не ssh-keyscan: встроенный в Windows клиент 9.5 не договаривается с
    # OpenSSH 10 на сервере, а его вывод в stderr здесь валит скрипт (Stop).
    $scan = & $venvPy (Join-Path $PSScriptRoot "fetch_host_key.py") $Server $SshPort
    if ($LASTEXITCODE -ne 0) {
        Warn "$scan"
        Warn "known_hosts не создан. Запустите установку снова, когда SSH на сервере будет готов."
    } else {
        Set-Content -Path $known -Value $scan -Encoding ascii
        Write-Host "Отпечаток ключа сервера — СВЕРЬТЕ с тем, что показал сервер (SFTP_1C.md, шаг 5):" -ForegroundColor Yellow
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
$count = & $venvPy -c "import markapp; from markapp.database import SessionLocal; from markapp.models import User; s=SessionLocal(); print(s.query(User).count())"
if ($count -eq "0") {
    $login = Read-Host "Логин первого пользователя"
    $pass = Read-Host "Пароль (не короче 8 символов)"
    & $venvPy (Join-Path $Root "create_user.py") $login $pass
}

# --- 7. Ярлык ---
$run = Join-Path $Root "deploy\run_marking.ps1"
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut($path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = "powershell.exe"
    $s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`" -Port $WebPort"
    $s.WorkingDirectory = $Root
    $s.Description = "Маркировка и поставки"
    $s.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Маркировка.lnk")
Ok "Ярлык «Маркировка» на рабочем столе"
if ($Autostart) {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath("Startup")) "Маркировка.lnk")
    Ok "Программа будет запускаться при входе в Windows"
}
Ok "Готово. Дальше: сервер по deploy\SFTP_1C.md, затем «Диагностика» -> «Отправить PING»."
