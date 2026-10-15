<#
.SYNOPSIS
    Рабочий компьютер: собрать скрипты рекламы (C:\direct) для переноса на сервер и ВЫКЛЮЧИТЬ их здесь.

.DESCRIPTION
    Как переезд «Репрайсера». Порядок важен: сначала здесь снимается расписание и ставится
    метка «переехали», и только потом снимаются копии баз. Два работающих экземпляра (здесь и
    на сервере) двигали бы ставки и пополняли бюджеты WB одновременно (двойные пополнения, а
    лимиты API WB у них общие), а копия, снятая с работающего скрипта, потеряла бы то, что он
    запишет после неё.

    1. Снимает задания AWER_WB_* из Планировщика.
    2. Кладёт MOVED_TO_SERVER.txt: ни один скрипт здесь больше не запустится (ни по расписанию,
       ни вручную) — каждый, начиная, видит метку и выходит.
    3. Ждёт, пока доработают уже запущенные скрипты (до -WaitMin минут).
    4. Снимает копии баз (wb_history.db, wb_rec.db) штатным механизмом SQLite и проверяет их.
    5. Из настроек AWER CRM берёт ТОЛЬКО то, что нужно скриптам Директа и Метрики (токен Яндекса,
       логин Директа, счётчик, соинвест), в crm_config.json — остальных ключей CRM в архиве нет.
    6. Собирает архив: скрипты, базы, состояние автопилота (пополнения за неделю, резервные
       копии), журналы, ваши таблицы (wb_cost.csv, wb_watch.csv …) и токены.

    В архиве токены WB и Яндекса. Переносить только по RDP (копировать-вставить файл в окно
    сервера), никуда не выкладывать, после установки на сервере удалить и здесь, и там.

    Сначала обычное обновление (в C:\direct должны быть свежие скрипты — с меткой «переехали»), потом:
        cd C:\sinh; git pull; Copy-Item C:\sinh\direct\*.py C:\direct\ -Force
        powershell -ExecutionPolicy Bypass -File C:\sinh\direct\deploy\export_for_server.ps1
    Папки deploy\ и tools\ и requirements.txt берутся из C:\sinh\direct (в C:\direct их обычно нет).

    Передумали ДО установки на сервере — удалите C:\direct\MOVED_TO_SERVER.txt и поставьте расписание
    снова: powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1
#>
[CmdletBinding()]
param(
    [string]$Root = "C:\direct",
    [string]$Out = "C:\temp\direct_move.zip",
    [int]$WaitMin = 40
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Repo = Split-Path $PSScriptRoot -Parent        # C:\sinh\direct — откуда взять deploy\, tools\, requirements.txt
Set-Location $Root
if (-not (Test-Path (Join-Path $Root "wb_bids.py"))) { Fail "В $Root нет wb_bids.py — это не папка скриптов (укажите -Root)" }
if (-not (Select-String -Path (Join-Path $Root "wb_common.py") -Pattern "MOVED_TO_SERVER" -Quiet)) {
    Fail "В $Root старые скрипты (не знают метку «переехали»). Сначала: cd C:\sinh; git pull; Copy-Item C:\sinh\direct\*.py C:\direct\ -Force"
}
if (-not (Test-Path (Join-Path $Repo "deploy\install_server.ps1"))) { Fail "Нет $Repo\deploy\install_server.ps1 — запускайте этот скрипт из C:\sinh\direct\deploy" }
if (-not (Test-Path (Join-Path $Root "wb_tokens.txt"))) { Fail "Нет $Root\wb_tokens.txt — без него на сервере скрипты не попадут в кабинеты WB" }
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Fail "Не найден python (тот, которым работают скрипты)" }
$mark = Join-Path $Root "MOVED_TO_SERVER.txt"

# --- 1. Снять расписание ---
Info "Снимаю расписание"
$tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "AWER*WB*" })
foreach ($t in $tasks) { Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false }
$left = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "AWER*WB*" })
if ($left.Count) { Fail "Не снялись задания: $(($left | ForEach-Object TaskName) -join ', ') — снимите их в Планировщике и запустите снова" }
Ok "Расписание снято ($($tasks.Count) заданий)"

# --- 2. Метка «переехали» ---
$stamp = Get-Date -Format "dd.MM.yyyy HH:mm"
Set-Content -Path $mark -Encoding UTF8 -Value @(
    "Скрипты перенесены на сервер $stamp (deploy\export_for_server.ps1).",
    "Работать — на сервере по RDP, в C:\direct; ярлык «Реклама WB» — на рабочем столе сервера.",
    "Здесь скрипты не запускаются: два компьютера меняли бы ставки и пополняли бюджеты одновременно.",
    "Передумали ДО установки на сервере: удалите этот файл и поставьте расписание снова (wb_schedule.ps1).")
Ok "Здесь скрипты больше не запустятся (MOVED_TO_SERVER.txt)"

# --- 3. Дождаться запущенных ---
$pattern = '(wb_|direct_|metrika_|webmaster_|yandex_|crm_)\w*\.py'
$lock = Join-Path $Root "wb_bids.running"
$deadline = (Get-Date).AddMinutes($WaitMin)
while ($true) {
    $busy = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match $pattern })
    if (-not $busy.Count -and -not (Test-Path $lock)) { break }
    if ((Get-Date) -gt $deadline) {
        Fail ("Ещё работает: " + (($busy | ForEach-Object { $_.CommandLine }) -join "; ") +
              $(if (Test-Path $lock) { "; блокировка wb_bids.running" } else { "" }) +
              ". Метка уже стоит — новые запуски не начнутся. Дождитесь конца и запустите этот скрипт снова.")
    }
    Info "Дорабатывает: $(($busy | ForEach-Object { ($_.CommandLine -split ' ' | Where-Object { $_ -match $pattern } | Select-Object -First 1) }) -join ', ') — жду…"
    Start-Sleep -Seconds 20
}
Ok "Запущенных скриптов нет"

# --- 4. Копии баз ---
$stage = Join-Path $env:TEMP ("direct_move_" + [guid]::NewGuid().ToString("N"))
$code = Join-Path $stage "direct"
New-Item -ItemType Directory -Force -Path $code | Out-Null
foreach ($db in @(Get-ChildItem -LiteralPath $Root -Filter "*.db" -File)) {
    Info "Копия базы $($db.Name)"
    $dst = Join-Path $code $db.Name
    & $py -c @"
import sqlite3, sys
src = sqlite3.connect(r'$($db.FullName)'); dst = sqlite3.connect(r'$dst')
src.backup(dst); dst.execute('PRAGMA journal_mode=DELETE')
ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
n = dst.execute('SELECT count(*) FROM sqlite_master WHERE type=?', ('table',)).fetchone()[0]
dst.close(); src.close()
print(f'  integrity {ok}, таблиц {n}')
sys.exit(0 if ok == 'ok' else 1)
"@
    if ($LASTEXITCODE -ne 0) { Fail "Копия $($db.Name) не прошла проверку — архив не собран" }
}
Ok "Копии баз сняты и проверены"

# --- 5. Настройки CRM для скриптов Директа (только нужные ключи) ---
$crm = Join-Path $env:USERPROFILE "AWER CRM\config.json"
if (Test-Path $crm) {
    & $py -c @"
import json, sys
src = json.load(open(r'$crm', encoding='utf-8-sig'))
keep = {k: src[k] for k in ('yandex_oauth_token', 'direct_client_login', 'metrika_counter_id', 'coinvest_multiplier') if src.get(k)}
json.dump(keep, open(r'$code\crm_config.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('  взято:', ', '.join(keep) or 'ничего')
"@
    if ($LASTEXITCODE -ne 0) { Fail "Не прочитались настройки AWER CRM ($crm)" }
    Ok "Настройки Яндекса для Директа и Метрики — в crm_config.json"
} else {
    Warn "Нет $crm — скрипты Директа и Метрики на сервере попросят токен Яндекса (впишите его в C:\direct\crm_config.json)"
}

# --- 6. Архив ---
Info "Собираю архив"
$skipDirs = @(".venv", "__pycache__", ".pytest_cache", "code_backups")
$skipNames = @("MOVED_TO_SERVER.txt", "wb_bids.running", "wb_collector.running", "wb_api.busy")
# Обход сам, а не Get-ChildItem -Recurse: тот заходит и в пропускаемые папки (урок «Репрайсера»).
function Copy-Tree($src, $dst) {
    foreach ($item in Get-ChildItem -LiteralPath $src -Force) {
        if ($item.PSIsContainer) {
            if ($skipDirs -contains $item.Name) { continue }
            Copy-Tree $item.FullName (Join-Path $dst $item.Name)
            continue
        }
        if (($skipNames -contains $item.Name) -or ($item.Name -match '\.(db|db-wal|db-shm|db-journal|tmp)$')) { continue }
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -LiteralPath $item.FullName (Join-Path $dst $item.Name)
    }
}
Copy-Tree $Root $code
if ($Repo -ne $Root) {             # установщик сервера, модель, список зависимостей — из репозитория
    foreach ($d in @("deploy", "tools")) {
        if (Test-Path (Join-Path $Repo $d)) { Copy-Tree (Join-Path $Repo $d) (Join-Path $code $d) }
    }
    foreach ($f in @("requirements.txt", "wb_schedule.ps1", "wb_dashboard.ico")) {
        if (Test-Path (Join-Path $Repo $f)) { Copy-Item -LiteralPath (Join-Path $Repo $f) (Join-Path $code $f) -Force }
    }
}
Set-Content -Path (Join-Path $code "EXPORTED.txt") -Encoding UTF8 -Value @(
    "Собрано $stamp на $env:COMPUTERNAME (deploy\export_for_server.ps1).",
    "Там скрипты остановлены и помечены MOVED_TO_SERVER.txt — второй экземпляр не работает.")
New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
if (Test-Path $Out) { Remove-Item $Out -Force }
# ZipFile, а не Compress-Archive: тот в PowerShell 5.1 пропускает скрытые файлы и портит русские имена.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($code, $Out)
Remove-Item $stage -Recurse -Force
$mb = [math]::Round((Get-Item $Out).Length / 1MB, 1)
Ok "Архив: $Out ($mb МБ)"

# --- 7. Ярлык дашборда здесь больше не нужен ---
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Реклама WB.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force; Ok "Ярлык «Реклама WB» здесь убран — дашборд будет на сервере" }

Warn "В архиве токены WB и Яндекса: переносите только по RDP, после установки на сервере удалите архив и здесь, и там."
Write-Host ""
Write-Host "Дальше на сервере (RDP, PowerShell ОТ АДМИНИСТРАТОРА):" -ForegroundColor Cyan
Write-Host "  1. Скопировать $Out на сервер в C:\temp\"
Write-Host "  2. Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::ExtractToDirectory('C:\temp\direct_move.zip', 'C:\direct')"
Write-Host "  3. powershell -ExecutionPolicy Bypass -File C:\direct\deploy\install_server.ps1"
