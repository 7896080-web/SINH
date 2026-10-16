<#
.SYNOPSIS
    Сервер: обновление скриптов рекламы в C:\direct из архива make_update.ps1 — с паузой расписания,
    копией прежнего кода и откатом.

.DESCRIPTION
    1. Распаковывает C:\temp\direct_update.zip во временную папку и проверяет, что новые скрипты
       собираются (compileall) — до того, как что-то тронуто.
    2. Выключает задания AWER_WB_* и ждёт, пока доработают запущенные скрипты (до -WaitMin мин):
       подменять файлы посреди применения ставок или пополнения нельзя.
    3. Копирует прежний код в C:\direct\code_backups\<время> и кладёт новый. Данные (базы, состояние
       автопилота, журналы, ваши таблицы, токены) не трогает: в архиве их нет.
    4. Зависимости, проверка, что скрипты загружаются, и снова расписание (wb_schedule.ps1 -Server).
       Не загрузились — возвращает прежний код и расписание сам.

    PowerShell от администратора:
        powershell -ExecutionPolicy Bypass -File C:\direct\deploy\update_server.ps1
    Откат к коду до последнего обновления:
        powershell -ExecutionPolicy Bypass -File C:\direct\deploy\update_server.ps1 -Rollback
#>
[CmdletBinding()]
param(
    [string]$Zip = "C:\temp\direct_update.zip",
    [int]$WaitMin = 30,
    [switch]$Rollback
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Запустите PowerShell от администратора." }
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — сначала deploy\install_server.ps1" }
$backups = Join-Path $Root "code_backups"

function Code-Files($dir) {
    # то, что везёт обновление: код, а не данные. Rel — путь от $dir (оба пути от одного Get-Item —
    # короткие имена вида ADMINI~1 в %TEMP% не сбивают отсчёт)
    $base = (Get-Item -LiteralPath $dir).FullName.TrimEnd("\")
    $files = @(Get-ChildItem -LiteralPath $base -File | Where-Object { $_.Extension -in @(".py", ".ps1", ".ico") -or $_.Name -in @("requirements.txt", "VERSION.txt") })
    foreach ($d in @("deploy", "tools")) {
        $p = Join-Path $base $d
        if (Test-Path $p) { $files += @(Get-ChildItem -LiteralPath $p -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' }) }
    }
    foreach ($f in $files) { [pscustomobject]@{ Full = $f.FullName; Rel = $f.FullName.Substring($base.Length + 1) } }
}

function Copy-Code($from, $to) {
    foreach ($f in @(Code-Files $from)) {
        $dst = Join-Path $to $f.Rel
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Copy-Item -LiteralPath $f.Full $dst -Force
    }
}

function Stop-Schedule {
    $tasks = @(Get-ScheduledTask -TaskName "AWER_WB_*" -ErrorAction SilentlyContinue)
    foreach ($t in $tasks) { Disable-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath | Out-Null }
    $pattern = '(wb_|direct_|metrika_|webmaster_|yandex_|crm_)\w*\.py'
    $deadline = (Get-Date).AddMinutes($WaitMin)
    while ($true) {
        $busy = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -match $pattern })
        if (-not $busy.Count -and -not (Test-Path (Join-Path $Root "wb_bids.running"))) { return $true }
        if ((Get-Date) -gt $deadline) { return $false }
        Info "Дорабатывает: $(@($busy | ForEach-Object { ([regex]::Match($_.CommandLine, $pattern)).Value }) -join ', ') — жду…"
        Start-Sleep -Seconds 20
    }
}

function Start-Schedule {
    # вывод — сразу на экран: иначе он стал бы частью результата функции, и любой непустой результат читался бы как успех
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "wb_schedule.ps1") -Server | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Test-Load {
    & $venvPy -m compileall -q -l $Root | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $venvPy -c "import wb_bids, wb_guard, wb_common, wb_collector, yandex_token" | Out-Host
    return ($LASTEXITCODE -eq 0)
}

if ($Rollback) {
    $last = Get-ChildItem -LiteralPath $backups -Directory -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    if (-not $last) { Fail "Копий прежнего кода нет ($backups)" }
    Info "Откат к коду из $($last.Name) (применяется сразу)"
    if (-not (Stop-Schedule)) { Start-Schedule | Out-Null; Fail "Скрипты работают дольше $WaitMin мин — откат не сделан, расписание включено обратно" }
    Copy-Code $last.FullName $Root
    if (-not (Test-Load)) { Warn "После отката скрипты не загружаются — смотрите сообщение выше" }
    if (-not (Start-Schedule)) { Fail "Расписание не поставилось — поставьте: powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1 -Server" }
    Ok "Код возвращён к $($last.Name), расписание включено"
    exit 0
}

# --- 1. Распаковать и проверить новое ---
if (-not (Test-Path $Zip)) { Fail "Нет $Zip — соберите его на рабочем компьютере (deploy\make_update.ps1) и скопируйте сюда" }
$stage = Join-Path $env:TEMP ("direct_upd_" + [guid]::NewGuid().ToString("N"))
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $stage)
if (-not (Test-Path (Join-Path $stage "wb_bids.py"))) { Fail "В $Zip нет wb_bids.py — это не архив make_update.ps1" }
foreach ($bad in @("wb_tokens.txt", "crm_config.json", "wb_bids_state.json")) {
    if (Test-Path (Join-Path $stage $bad)) { Fail "В архиве обновления есть $bad — это не обновление кода, ничего не делаю" }
}
& $venvPy -m compileall -q -l $stage | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Новые скрипты не собираются — ничего не тронуто" }
$old = if (Test-Path (Join-Path $Root "VERSION.txt")) { (Get-Content (Join-Path $Root "VERSION.txt") -Encoding UTF8 -TotalCount 1) -join "" } else { "неизвестна" }
$new = if (Test-Path (Join-Path $stage "VERSION.txt")) { (Get-Content (Join-Path $stage "VERSION.txt") -Encoding UTF8 -TotalCount 1) -join "" } else { "неизвестна" }
Info "Версия: $old  ->  $new"

# --- 2. Пауза расписания ---
Info "Выключаю расписание и жду, пока доработают скрипты"
if (-not (Stop-Schedule)) { Start-Schedule | Out-Null; Fail "Скрипты работают дольше $WaitMin мин — обновление не сделано, расписание включено обратно. Запустите позже." }

# --- 3. Копия прежнего кода и замена ---
$bk = Join-Path $backups (Get-Date -Format "yyyyMMdd_HHmmss")
Copy-Code $Root $bk
Ok "Прежний код — в $bk"
Copy-Code $stage $Root
Remove-Item $stage -Recurse -Force

# --- 4. Зависимости, проверка, расписание ---
$req = Join-Path $Root "requirements.txt"
if (Test-Path $req) { & $venvPy -m pip install --disable-pip-version-check -q -r $req }
if (-not (Test-Load)) {
    Warn "Новые скрипты не загружаются — возвращаю прежний код"
    Copy-Code $bk $Root
    Start-Schedule | Out-Null
    Fail "Обновление отменено, работает прежняя версия ($old)"
}
if (-not (Start-Schedule)) { Fail "Расписание не поставилось — поставьте: powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1 -Server" }
# ярлыки: новые страницы (например, «Бюджеты WB») появляются на рабочем столе сами
$sc = Join-Path $Root "deploy\shortcuts.ps1"
if (Test-Path $sc) { & powershell -NoProfile -ExecutionPolicy Bypass -File $sc -Root $Root | Out-Host }
# старые копии кода — оставить последние 10
Get-ChildItem -LiteralPath $backups -Directory | Sort-Object Name | Select-Object -SkipLast 10 | Remove-Item -Recurse -Force
Ok "Обновлено до $new, расписание включено"
