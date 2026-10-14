# Установка и обновление финансового помощника (Telegram-бот) на Windows.
#
#   Запуск: PowerShell «от имени администратора», из распакованной папки:
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
#
# Первая установка: при необходимости ставит Python, кладёт код в C:\FinanceBot,
# создаёт виртуальное окружение, задание Планировщика «FinanceBot» (держит ботов
# запущенными, стартует вместе с Windows) и ежедневный бэкап «FinanceBot-Backup».
# Повторный запуск = обновление: код и зависимости заменяются, .env и данные
# (data\, data-test\) не трогаются. Перед обновлением делается бэкап баз.
param([string]$App = "C:\FinanceBot")

$ErrorActionPreference = "Continue"   # ошибки внешних команд проверяем сами
$App = $App.TrimEnd("\")               # «C:\FinanceBot\» сломал бы кавычки в задании
$Stopped = $false
$Task = "FinanceBot"
$BackupTask = "FinanceBot-Backup"
$PythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"

function Say($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host "`nОшибка: $text" -ForegroundColor Red
    if ($Stopped) {
        Write-Host "Бот сейчас остановлен. Исправьте причину и запустите установку снова" -ForegroundColor Yellow
        Write-Host "(или запустите прежнюю версию: deploy\windows\stop.ps1 -Start)." -ForegroundColor Yellow
    }
    exit 1
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "запустите PowerShell от имени администратора (правой кнопкой → «Запуск от имени администратора»)"
}
$Src = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $Src "finance\bot.py"))) {
    Fail "не нашёл finance\bot.py — запускайте из распакованной папки finance_assistant"
}

function Find-Python {
    $candidates = @()
    # Сначала — Python для всех пользователей (Program Files).
    Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $candidates += ,@($_.FullName) }
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $rest = @($c | Select-Object -Skip 1)
        try {
            $out = & $exe @rest -c "import sys; print(sys.version_info >= (3, 10), sys.executable)" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and "$out" -match '^True (.+)$') { return $Matches[1].Trim() }
    }
    return $null
}

function Set-FolderAccess {
    # Доступ к папке — только у системы и администраторов: там токены и базы.
    # SID вместо имён: на русской Windows группа называется «Администраторы».
    # Права ставятся только на саму папку, вложенное их наследует. (Прежняя
    # версия ставила их с /T на каждый файл и оставляла файлы без доступа
    # вовсе — сначала возвращаем владельца и наследование, это исправляет.)
    # Порядок важен: сначала закрываем саму папку, потом вложенное наследует
    # только её права (иначе на миг открылось бы наследование от C:\ для всех).
    & icacls $App /setowner "*S-1-5-32-544" /T /C /Q 2>&1 | Out-Null
    & icacls $App /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /C /Q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "не удалось ограничить доступ к $App (там будут токены и базы)" }
    if (Get-ChildItem $App -Force -ErrorAction SilentlyContinue) {
        & icacls "$App\*" /reset /T /C /Q 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "не удалось выставить права на файлы в $App" }
    }
}

function Get-BotProcesses {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '-m\s+finance(\.supervise|\.settings_web)?(\s|$)' -and
                       $_.CommandLine -notmatch 'set-password' }
}

function Stop-Bot {
    # Отключаем задание: иначе его проверка раз в 5 минут запустила бы бота
    # посреди обновления (на наполовину скопированном коде).
    Disable-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue | Out-Null
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    # Процессы, запущенные заданием, при его остановке сами не завершаются.
    for ($i = 0; $i -lt 15; $i++) {
        $left = @(Get-BotProcesses)
        if (-not $left) { return }
        $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
    }
    Fail "не удалось остановить процессы бота — перезагрузите сервер и запустите установку снова"
}

Say "Проверяю Python"
$Py = Find-Python
$Profiles = Split-Path -Parent $env:PUBLIC   # обычно C:\Users
if ($Py -and $Py.StartsWith($Profiles, [StringComparison]::OrdinalIgnoreCase)) {
    # Python из профиля пользователя: задание работает от имени системы, а файлы
    # в профиле может менять этот пользователь без прав администратора, и при
    # удалении профиля или Python бот перестал бы запускаться.
    Write-Host "   найден Python в профиле пользователя ($Py) — для службы нужен общий"
    $Py = $null
}
if (-not $Py) {
    Say "Ставлю Python 3.12 для всех пользователей"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $installer = Join-Path $env:TEMP "python-3.12-amd64.exe"
    try {
        Invoke-WebRequest -Uri $PythonUrl -OutFile $installer -UseBasicParsing
    } catch {
        Fail "не удалось скачать Python ($($_.Exception.Message)). Установите вручную с python.org (Windows installer 64-bit), отметьте «Add python.exe to PATH» и «Install for all users», затем запустите эту установку снова."
    }
    $p = Start-Process -FilePath $installer -Wait -PassThru `
        -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_launcher=1", "Include_test=0"
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Fail "установщик Python завершился с кодом $($p.ExitCode)" }
    $Py = Find-Python
    if ($Py -and $Py.StartsWith($Profiles, [StringComparison]::OrdinalIgnoreCase)) {
        $Py = Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Py) { Fail "Python установлен, но не найден — откройте новое окно PowerShell и запустите установку снова" }
}
Write-Host "   $Py"

$Update = Test-Path (Join-Path $App "finance")
$VPy = Join-Path $App "venv\Scripts\python.exe"
if (Test-Path $App) {
    Say "Проверяю права на $App"
    Set-FolderAccess
}

if ($Update) {
    Say "Обновление: останавливаю бота"
    $Stopped = $true
    Stop-Bot
    if ((Test-Path $VPy) -and (Test-Path (Join-Path $App "finance\backup.py")) -and (Test-Path (Join-Path $App "data"))) {
        Say "Обновление: сначала бэкап баз"
        Push-Location $App
        & $VPy -m finance.backup --data (Join-Path $App "data") --dest (Join-Path $App "backups")
        $rc = $LASTEXITCODE
        Pop-Location
        if ($rc -ne 0) { Fail "бэкап не удался — обновление остановлено, бот не запущен. Запустите установку ещё раз." }
    }
}

Say "Копирую код в $App"
New-Item -ItemType Directory -Force -Path $App | Out-Null
$SameFolder = (Resolve-Path $Src).Path.TrimEnd("\") -eq (Resolve-Path $App).Path.TrimEnd("\")
if ($SameFolder) {
    Write-Host "   запуск из $App — код уже на месте, обновляю только зависимости и задания"
}
foreach ($dir in $(if ($SameFolder) { @() } else { "finance", "deploy" })) {
    $target = Join-Path $App $dir
    if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
    if (Test-Path $target) { Fail "не удалось удалить старую папку $target (файл занят?)" }
    Copy-Item -Recurse (Join-Path $Src $dir) $target
}
Get-ChildItem (Join-Path $App "finance") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
if (-not $SameFolder) {
    Copy-Item (Join-Path $Src "requirements.txt"), (Join-Path $Src "constraints.txt"), (Join-Path $Src ".env.example") $App -Force
    foreach ($doc in "README.md", "УСТАНОВКА-WINDOWS.md") {
        if (Test-Path (Join-Path $Src $doc)) { Copy-Item (Join-Path $Src $doc) $App -Force }
    }
}

Say "Ставлю зависимости (виртуальное окружение $App\venv)"
$VenvCfg = Join-Path $App "venv\pyvenv.cfg"
if ((Test-Path $VenvCfg) -and ((Get-Content $VenvCfg -Encoding UTF8) -match "^home\s*=\s*$([regex]::Escape($Profiles))")) {
    Write-Host "   окружение было на Python из профиля пользователя — пересоздаю"
    Remove-Item -Recurse -Force (Join-Path $App "venv")
}
if (-not (Test-Path $VPy)) {
    & $Py -m venv (Join-Path $App "venv")
    if ($LASTEXITCODE -ne 0) { Fail "не удалось создать виртуальное окружение" }
}
& $VPy -m pip install --quiet --disable-pip-version-check --upgrade pip
& $VPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $App "requirements.txt") `
    -c (Join-Path $App "constraints.txt")
if ($LASTEXITCODE -ne 0) { Fail "не удалось поставить зависимости — см. ошибку выше (нужен доступ в интернет)" }
Push-Location $App
& $VPy -c "import finance.bot, finance.supervise, finance.windows, finance.backup, psutil, cryptography, tzdata"
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Fail "код не импортируется — см. ошибку выше" }

Say "Папки данных и настройки"
foreach ($dir in "data", "data-test", "logs", "settings", "backups") {
    New-Item -ItemType Directory -Force -Path (Join-Path $App $dir) | Out-Null
}
Push-Location $App
& $VPy -m finance.windows init-env $App | Out-Null
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Fail "не удалось подготовить .env" }
Set-FolderAccess

Say "Задания Планировщика"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -Priority 4
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$action = New-ScheduledTaskAction -Execute $VPy -Argument "-m finance.supervise --app `"$App`"" -WorkingDirectory $App
# При старте Windows + каждые 5 минут: если супервизор почему-то завершился,
# задание запустит его снова (а работающий второй раз не запускается).
$triggers = @((New-ScheduledTaskTrigger -AtStartup),
              (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)))
try {
    Register-ScheduledTask -TaskName $Task -Action $action -Principal $principal -Settings $settings `
        -Trigger $triggers -Force -ErrorAction Stop `
        -Description "Финансовый помощник: боевой и тестовый бот, страница настроек" | Out-Null
} catch { Fail "не удалось создать задание $Task : $($_.Exception.Message)" }

$backupAction = New-ScheduledTaskAction -Execute $VPy -WorkingDirectory $App `
    -Argument "-m finance.backup --data `"$App\data`" --dest `"$App\backups`" --env `"$App\.env`""
$backupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
try {
    Register-ScheduledTask -TaskName $BackupTask -Action $backupAction -Principal $principal `
        -Settings $backupSettings -Trigger (New-ScheduledTaskTrigger -Daily -At "03:30") -Force `
        -ErrorAction Stop -Description "Финансовый помощник: ежедневный бэкап баз" | Out-Null
} catch { Fail "не удалось создать задание $BackupTask : $($_.Exception.Message)" }

# Сертификат страницы настроек живёт 2 года — при обновлении продлеваем заранее.
if (Test-Path (Join-Path $App "settings\enabled")) {
    Push-Location $App
    $certOut = "$(& $VPy -m finance.windows cert (Join-Path $App "settings"))"
    Pop-Location
    if ($certOut -match "Создан") { Write-Host "   сертификат страницы настроек обновлён" }
}

Say "Запускаю"
Enable-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue | Out-Null
Start-ScheduledTask -TaskName $Task
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $Task).State
if ("$state" -ne "Running") {
    $result = (Get-ScheduledTaskInfo -TaskName $Task).LastTaskResult
    Get-Content (Join-Path $App "logs\supervisor.log") -Tail 30 -Encoding UTF8 -ErrorAction SilentlyContinue
    Push-Location $App
    & $VPy -c "import finance.supervise; print('   запуск от администратора: код импортируется')"
    Pop-Location
    Fail ("задание $Task не работает (состояние: $state, код 0x{0:X8}) — пришлите этот вывод" -f $result)
}
Write-Host "   задание $Task работает; журналы: $App\logs"

Push-Location $App
$missing = "$(& $VPy -m finance.windows missing $App)".Trim()
Pop-Location
$settingsScript = Join-Path $App "deploy\windows\settings.ps1"
if ($missing) {
    Say "Осталось вписать настройки"
    Write-Host "   Не заполнено: $missing"
    Write-Host "   Включите страницу настроек (спросит пароль и покажет адрес):"
    Write-Host ""
    Write-Host "      powershell -ExecutionPolicy Bypass -File `"$settingsScript`""
    Write-Host ""
    Write-Host "   На ней — ключ Claude API, токен бота, токен тестового бота и Telegram id"
    Write-Host "   обоих пользователей. После сохранения боты запустятся сами."
} else {
    Say "Готово"
    if ($Update) { Write-Host "   Обновлено. Данные и .env не тронуты." }
    Write-Host "   Журналы:  $App\logs  (bot.log, bot-test.log, settings.log)"
    Write-Host "   Состояние: powershell -ExecutionPolicy Bypass -File `"$App\deploy\windows\status.ps1`""
    Write-Host "   Бэкап:    каждый день в 03:30 → $App\backups"
}
