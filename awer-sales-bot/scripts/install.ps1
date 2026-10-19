# Установка и обновление awer-sales-bot на Windows Server (docs/УСТАНОВКА.md).
#
#   PowerShell «от имени администратора», из распакованной папки awer-sales-bot:
#     powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# Первая установка: при необходимости ставит Python 3.12, кладёт программу в C:\awer-sales-bot, создаёт .venv,
# config.yaml и .env (пароль админки показывается один раз), базу, загружает файлы 1С из data\inbox, проверяет
# тестами, регистрирует службу AwerSalesBot (NSSM, как у Sync Admin) и задачу копии базы в облако.
# Повторный запуск из новой распакованной версии = обновление: снимок базы → замена кода → тесты → запуск;
# не прошли тесты или служба не поднялась — возвращается прежний код. data\, .env, config.yaml, rclone.conf
# и собранная обработка onec\*.epf не трогаются.
param(
    [string]$App = "C:\awer-sales-bot",
    [int]$Port = 8080,
    [string]$Service = "AwerSalesBot",
    [string[]]$Admins = @(),      # «Фамилия Имя» старших продавцов — роль после загрузки истории
    [switch]$SkipTests
)

$ErrorActionPreference = "Continue"   # ошибки внешних команд проверяем сами по $LASTEXITCODE
$App = $App.TrimEnd("\")
# powershell -File передаёт -Admins "А","Б" одной строкой «А,Б» — делим сами
$Admins = @($Admins | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$BackupTask = "AwerSalesBot cloud backup"
$PythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$Epf = "ВыгрузкаПродажДляБота.epf"
$CodeDirs = @("src", "content", "scripts", "docs", "tests", "onec\ВыгрузкаПродажДляБота")
$CodeFiles = @("pyproject.toml", "config.example.yaml", ".env.example", "README.md")
$script:Stopped = $false
$script:OldCode = $null

function Say($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" }
function Warn($text) { Write-Host "    ! $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host "`nОшибка: $text" -ForegroundColor Red
    if ($script:Stopped) {
        Write-Host "Служба $Service остановлена. Исправьте причину и запустите установку снова." -ForegroundColor Yellow
    }
    exit 1
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "запустите PowerShell от имени администратора (правой кнопкой → «Запуск от имени администратора»)"
}
$Src = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Src "src\awer_sales_bot\cli.py"))) {
    Fail "не нашёл src\awer_sales_bot\cli.py — запускайте scripts\install.ps1 из распакованной папки awer-sales-bot"
}
$VPy = Join-Path $App ".venv\Scripts\python.exe"

function Find-Python {
    # Python для всех пользователей (Program Files) — служба работает от имени системы, ей нужен общий.
    $candidates = @()
    Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $candidates += ,@($_.FullName) }
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
    $profiles = Split-Path -Parent $env:PUBLIC   # обычно C:\Users
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        try {
            $out = & $exe @rest -c "import sys; print(sys.version_info >= (3, 11), sys.executable)" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and "$out" -match '^True (.+)$') {
            $path = $Matches[1].Trim()
            if (-not $path.StartsWith($profiles, [StringComparison]::OrdinalIgnoreCase)) { return $path }
        }
    }
    return $null
}

function Find-Nssm {
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in "C:\nssm\win64\nssm.exe", "C:\nssm\nssm.exe", "C:\sync_admin\tools\nssm.exe", "C:\tools\nssm.exe") {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Invoke-Bot([string[]]$BotArgs) {
    # Команда awer-bot из папки программы (config.yaml и data\ — относительно неё).
    # Вывод — сразу на экран (Out-Host): иначе он стал бы частью возвращаемого значения и сломал проверку кода.
    Push-Location $App
    & $VPy -m awer_sales_bot.cli @BotArgs | Out-Host
    $rc = $LASTEXITCODE
    Pop-Location
    return $rc
}

function Lock-Secret($path) {
    # .env и rclone.conf — только система и администраторы (SID: на русской Windows «Администраторы»).
    if (Test-Path $path) {
        & icacls $path /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" /C /Q 2>&1 | Out-Null
    }
}

function Copy-Code($from, $to) {
    foreach ($d in $CodeDirs) {
        $target = Join-Path $to $d
        if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
        if (Test-Path $target) { return "не удалось удалить $target (файл занят?)" }
        if (Test-Path (Join-Path $from $d)) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -Recurse (Join-Path $from $d) $target
        }
    }
    foreach ($f in $CodeFiles) {
        if (Test-Path (Join-Path $from $f)) { Copy-Item (Join-Path $from $f) (Join-Path $to $f) -Force }
    }
    Get-ChildItem $to -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    return $null
}

function Install-Deps {
    Push-Location $App
    & $VPy -m pip install --quiet --disable-pip-version-check -e ".[dev]" | Out-Host
    $rc = $LASTEXITCODE
    Pop-Location
    return $rc
}

function Restore-OldCode($why) {
    if (-not $script:OldCode) { Fail $why }
    Warn "$why — возвращаю прежнюю версию из $($script:OldCode)"
    $err = Copy-Code $script:OldCode $App
    if ($err) { Fail "$why; вернуть прежний код не удалось: $err" }
    Install-Deps | Out-Null
    & $Nssm start $Service | Out-Null
    Fail "$why. Прежняя версия возвращена и запущена; пришлите этот вывод разработчику."
}

function Test-Web {
    # Админка жива, если отвечает: 401 (просит пароль) — тоже ответ.
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 5 -MaximumRedirection 0
            return $true
        } catch {
            $resp = $_.Exception.Response
            if ($resp -and ([int]$resp.StatusCode -in 302, 303, 307, 401)) { return $true }
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

# ------------------------------------------------------------------ Python и NSSM
Say "Проверяю Python 3.11+"
$Py = Find-Python
if (-not $Py) {
    Say "Ставлю Python 3.12 для всех пользователей"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $installer = Join-Path $env:TEMP "python-3.12-amd64.exe"
    try {
        Invoke-WebRequest -Uri $PythonUrl -OutFile $installer -UseBasicParsing
    } catch {
        Fail "не удалось скачать Python ($($_.Exception.Message)). Поставьте вручную с python.org: «Install for all users» и «Add python.exe to PATH», затем запустите установку снова."
    }
    $p = Start-Process -FilePath $installer -Wait -PassThru `
        -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_launcher=1", "Include_test=0"
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Fail "установщик Python завершился с кодом $($p.ExitCode)" }
    $Py = Find-Python
    if (-not $Py) { Fail "Python установлен, но не найден — откройте новое окно PowerShell и запустите установку снова" }
}
Note $Py
$Nssm = Find-Nssm
if (-not $Nssm) {
    Fail "не найден nssm.exe (им же запущены службы Sync Admin). Скачайте https://nssm.cc/download, положите win64\nssm.exe в C:\nssm\win64\ и запустите установку снова."
}
Note "NSSM: $Nssm"

$svc = Get-Service -Name $Service -ErrorAction SilentlyContinue
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy -and -not ($svc -and $svc.Status -eq "Running")) {
    $who = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).Path
    Fail "порт $Port уже занят ($who). Запустите установку с другим портом: -Port 8090"
}

# ------------------------------------------------------------------ обновление: остановка, снимок, копия кода
$Update = Test-Path (Join-Path $App "src\awer_sales_bot")
$SameFolder = (Test-Path $App) -and ((Resolve-Path $Src).Path.TrimEnd("\") -eq (Resolve-Path $App).Path.TrimEnd("\"))
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
foreach ($dir in "data\inbox", "data\archive", "logs", "backups") {
    New-Item -ItemType Directory -Force -Path (Join-Path $App $dir) | Out-Null
}
if ($Update) {
    Say "Обновление: останавливаю службу и делаю снимок базы"
    if ($svc) {
        & $Nssm stop $Service | Out-Null
        $script:Stopped = $true
    }
    if ((Test-Path $VPy) -and (Test-Path (Join-Path $App "data\awer.db"))) {
        $rc = Invoke-Bot @("backup", "--dest", "backups", "--log", "logs\backup.log")
        if ($rc -ne 0) {
            if ($svc) { & $Nssm start $Service | Out-Null }
            Fail "снимок базы не удался — обновление отменено, служба запущена как была (logs\backup.log)"
        }
    }
    if (-not $SameFolder) {
        $script:OldCode = Join-Path $App "backups\code-$stamp"
        New-Item -ItemType Directory -Force -Path $script:OldCode | Out-Null
        $err = Copy-Code $App $script:OldCode
        if ($err) { Fail "не удалось сохранить прежний код: $err" }
        Note "прежний код: $($script:OldCode)"
    }
}

if ($SameFolder) {
    Say "Запуск из $App — код уже на месте, обновляю зависимости, службу и задачи"
} else {
    Say "Копирую программу в $App"
    $err = Copy-Code $Src $App
    if ($err) { if ($Update) { Restore-OldCode $err } else { Fail $err } }
}
$epfPath = Join-Path $App "onec\$Epf"
if (-not (Test-Path $epfPath)) {
    foreach ($p in (Join-Path $Src "onec\$Epf"), "C:\sync_admin\1c\$Epf") {
        if (Test-Path $p) { Copy-Item $p $epfPath; Note "обработка 1С: $p → $epfPath"; break }
    }
}
if (-not (Test-Path $epfPath)) { Warn "нет $epfPath — соберите обработку (docs/1C.md) до настройки задачи 1С" }

# ------------------------------------------------------------------ окружение и зависимости
Say "Виртуальное окружение и зависимости ($App\.venv)"
$venvCfg = Join-Path $App ".venv\pyvenv.cfg"
$profiles = Split-Path -Parent $env:PUBLIC
if ((Test-Path $venvCfg) -and ((Get-Content $venvCfg -Encoding UTF8) -match "^home\s*=\s*$([regex]::Escape($profiles))")) {
    Note "окружение было на Python из профиля пользователя — пересоздаю"
    Remove-Item -Recurse -Force (Join-Path $App ".venv")
}
if (-not (Test-Path $VPy)) {
    & $Py -m venv (Join-Path $App ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "не удалось создать виртуальное окружение" }
}
& $VPy -m pip install --quiet --disable-pip-version-check --upgrade pip
if ((Install-Deps) -ne 0) {
    $why = "не удалось поставить зависимости (нужен доступ в интернет) — см. ошибку выше"
    if ($Update) { Restore-OldCode $why } else { Fail $why }
}

# ------------------------------------------------------------------ настройки
Say "Настройки"
$cfgPath = Join-Path $App "config.yaml"
if (-not (Test-Path $cfgPath)) {
    Copy-Item (Join-Path $App "config.example.yaml") $cfgPath
    Note "config.yaml создан из config.example.yaml"
} else { Note "config.yaml уже есть — не трогаю" }
$envPath = Join-Path $App ".env"
$AdminPassword = $null
if (-not (Test-Path $envPath)) {
    $alphabet = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object 'System.Byte[]' 48
    $rng.GetBytes($bytes)
    $AdminPassword = -join ($bytes[0..13] | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
    $upload = -join ($bytes[14..45] | ForEach-Object { "{0:x2}" -f $_ })
    $lines = @(
        "# Секреты awer-sales-bot. Не пересылать и не класть в облако.",
        "ADMIN_PASSWORD=$AdminPassword",
        "UPLOAD_TOKEN=$upload",
        "# токены ботов вписываются в админке: «Бот MAX»",
        "MAX_BOT_TOKEN=",
        "MAX_TRAINING_BOT_TOKEN=",
        "ANTHROPIC_API_KEY="
    )
    # без BOM: так .env читают и бот, и Блокнот
    [System.IO.File]::WriteAllLines($envPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
    Note ".env создан"
} else { Note ".env уже есть — не трогаю (пароль и токены прежние)" }
Lock-Secret $envPath

# ------------------------------------------------------------------ проверка тестами
if (-not $SkipTests) {
    Say "Проверка программы тестами (1–2 минуты)"
    Push-Location $App
    & $VPy -m pytest -q -p no:cacheprovider
    $rc = $LASTEXITCODE
    Pop-Location
    if ($rc -ne 0) {
        if ($Update) { Restore-OldCode "тесты не прошли" }
        Warn "тесты не прошли (вывод выше). Данных в системе ещё нет, установка продолжается — пришлите вывод разработчику."
    }
}

# ------------------------------------------------------------------ база и история 1С
Say "База и данные 1С"
if ((Invoke-Bot @("init-db")) -ne 0) { if ($Update) { Restore-OldCode "база не открылась" } else { Fail "база не создана" } }
$waiting = @(Get-ChildItem (Join-Path $App "data\inbox") -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike ".*" })
if ($waiting.Count -gt 0) {
    Note "в data\inbox $($waiting.Count) файл(ов) из 1С — загружаю (история может грузиться несколько минут)"
    if ((Invoke-Bot @("import-sales")) -ne 0) { Warn "загрузка из data\inbox завершилась с ошибкой — см. выше" }
}
foreach ($name in $Admins) {
    if ((Invoke-Bot @("set-role", $name, "admin")) -eq 0) { Note "старший: $name" } else { Warn "не нашёл продавца «$name» — роль поставьте в админке «Продавцы»" }
}

# ------------------------------------------------------------------ служба
Say "Служба $Service (NSSM)"
if (-not (Get-Service -Name $Service -ErrorAction SilentlyContinue)) {
    & $Nssm install $Service $VPy | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "nssm install $Service не удался" }
}
& $Nssm set $Service Application $VPy | Out-Null
& $Nssm set $Service AppParameters "-m awer_sales_bot.cli serve --host 127.0.0.1 --port $Port" | Out-Null
& $Nssm set $Service AppDirectory $App | Out-Null
# имя и описание латиницей: службы Windows и NSSM на этом сервере кириллицу показывают не везде
& $Nssm set $Service DisplayName "AWER sales bot (MAX)" | Out-Null
& $Nssm set $Service Description "awer-sales-bot: admin http://127.0.0.1:$Port, MAX bot, 1C data in $App" | Out-Null
& $Nssm set $Service AppStdout (Join-Path $App "logs\service.log") | Out-Null
& $Nssm set $Service AppStderr (Join-Path $App "logs\service.err.log") | Out-Null
& $Nssm set $Service AppRotateFiles 1 | Out-Null
& $Nssm set $Service AppRotateOnline 1 | Out-Null
& $Nssm set $Service AppRotateBytes 10485760 | Out-Null
& $Nssm set $Service AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8" | Out-Null
& $Nssm set $Service Start SERVICE_AUTO_START | Out-Null
# поток бота упал — программа завершается сама (bot.guarded); NSSM поднимает её через 10 секунд
& $Nssm set $Service AppExit Default Restart | Out-Null
& $Nssm set $Service AppRestartDelay 10000 | Out-Null
& $Nssm start $Service | Out-Null
$script:Stopped = $false
if (-not (Test-Web)) {
    Get-Content (Join-Path $App "logs\service.err.log") -Tail 30 -Encoding UTF8 -ErrorAction SilentlyContinue
    if ($Update) { & $Nssm stop $Service | Out-Null; Restore-OldCode "служба не отвечает на http://127.0.0.1:$Port" }
    Fail "служба не отвечает на http://127.0.0.1:$Port — последние строки журнала выше"
}
Note "служба работает: http://127.0.0.1:$Port"

# ------------------------------------------------------------------ копия базы в облако (BACKUPS.md)
Say "Задача «$BackupTask» (каждый день в 05:00)"
$rclone = Join-Path $App "rclone.conf"
if (-not (Test-Path $rclone)) {
    foreach ($p in "C:\FinanceBot\rclone.conf", "C:\repricer\rclone.conf") {
        if ((Test-Path $p) -and (Select-String -Path $p -Pattern "^\[selectel\]" -Quiet)) {
            Copy-Item $p $rclone
            Note "rclone.conf скопирован из $p (тот же ключ Selectel, remote selectel; файл остаётся на сервере)"
            break
        }
    }
}
Lock-Secret $rclone
if (-not (Test-Path $rclone)) { Warn "нет $rclone — копии будут только в $App\backups (docs/УСТАНОВКА.md, «Копии базы»)" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $App `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$App\scripts\cloud_copy.ps1`" -App `"$App`""
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName $BackupTask -Action $action -Principal $principal -Settings $settings `
        -Trigger (New-ScheduledTaskTrigger -Daily -At "05:00") -Force -ErrorAction Stop `
        -Description "awer-sales-bot: снимок data\awer.db → backups\ (30 дней) → selectel:awer-backups/awer_sales_bot" | Out-Null
    Start-ScheduledTask -TaskName $BackupTask
    Note "первая копия запущена; журнал: $App\logs\cloud_copy.log"
} catch { Warn "задача копии не создана: $($_.Exception.Message)" }

# ------------------------------------------------------------------ итог
Say "Состояние"
Invoke-Bot @("doctor", "--backups", "backups") | Out-Null
Say "Готово"
if ($Update) { Note "Обновлено. Данные, .env и config.yaml не тронуты; снимок базы — $App\backups." }
Note "Админка:  http://127.0.0.1:$Port   (логин admin)"
if ($AdminPassword) {
    Write-Host "    Пароль админки: $AdminPassword" -ForegroundColor Green
    Write-Host "    Запишите его — он показан один раз (хранится в $App\.env)." -ForegroundColor Yellow
}
Note "Состояние:  powershell -ExecutionPolicy Bypass -File $App\scripts\status.ps1"
Note "Дальше — docs\УСТАНОВКА.md: токены ботов, владелец, закрытые дни, задача 1С (scripts\install-1c-task.ps1)."
