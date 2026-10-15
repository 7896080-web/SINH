<#
.SYNOPSIS
    Сервер (Windows, рядом с 1С, sync_admin и «Репрайсером»): установка скриптов рекламы в C:\direct.

.DESCRIPTION
    Перенос с рабочего компьютера: там deploy\export_for_server.ps1 остановил скрипты, снял копии
    баз и собрал архив; здесь он распакован в C:\direct.

    1. Проверяет: права администратора, файлы на месте, это не каталог sync_admin / «Репрайсера»,
       часовой пояс (расписание — по времени сервера).
    2. Python 3.11+ (тот, что стоит для sync_admin), СВОЙ venv C:\direct\.venv и requests.
       sync_admin, «Репрайсер», 1С и их Python не трогает. Служб и портов нет — только задания
       Планировщика.
    3. Права: задания идут от SYSTEM, файлы, которые они создают, вы правите из своей учётной
       записи — даёт ей «Изменение» на C:\direct.
    4. Проверяет базы (integrity_check) и что все скрипты собираются.
    5. Пробный прогон частой проверки лимита (ничего не меняет): видно, что с сервера есть связь с WB
       и токены работают.
    6. Спрашивает и ставит расписание от SYSTEM (wb_schedule.ps1 -Server): работает без входа в
       систему, выход из RDP его не останавливает.
    7. Ярлык «Реклама WB» на общем рабочем столе — через Chrome/Edge, не через Internet Explorer
       (на Windows Server он браузер по умолчанию, графики дашборда в нём не работают).

    Повторный запуск безопасен.

    PowerShell ОТ АДМИНИСТРАТОРА:
        powershell -ExecutionPolicy Bypass -File C:\direct\deploy\install_server.ps1
#>
[CmdletBinding()]
param([switch]$NoSchedule)
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
if ($Root -like "*sync_admin*" -or $Root -like "*repricer*") { Fail "$Root — каталог другой программы. Скрипты рекламы ставятся в свой каталог, C:\direct." }
if (-not (Test-Path (Join-Path $Root "wb_bids.py"))) { Fail "В $Root нет wb_bids.py — распакуйте архив переноса в $Root." }
if (-not (Test-Path (Join-Path $Root "wb_tokens.txt"))) { Fail "Нет $Root\wb_tokens.txt — он приходит в архиве переноса (export_for_server.ps1 на рабочем компьютере)." }
$exported = Join-Path $Root "EXPORTED.txt"
if (Test-Path $exported) { Info ((Get-Content $exported -Encoding UTF8 -TotalCount 1) -join "") }
else { Warn "Нет EXPORTED.txt — это не архив export_for_server.ps1. Убедитесь, что на рабочем компьютере скрипты остановлены: два экземпляра меняли бы рекламу одновременно." }
$mark = Join-Path $Root "MOVED_TO_SERVER.txt"
if (Test-Path $mark) { Remove-Item $mark -Force }
Info "Каталог: $Root"

# --- 1. Часовой пояс ---
$tz = (Get-TimeZone).Id
if ($tz -ne "Russian Standard Time") { Warn "Часовой пояс сервера — $tz, не московский: расписание (8:00, 13:00, 18:00 …) пойдёт по времени сервера." }
else { Ok "Часовой пояс московский" }

# --- 2. Python, venv ---
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
Info "Ставлю requests"
$req = Join-Path $Root "requirements.txt"
if (Test-Path $req) { & $venvPy -m pip install --disable-pip-version-check -q -r $req }
else { & $venvPy -m pip install --disable-pip-version-check -q requests }
if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой (нет доступа к PyPI?)" }
Ok "Зависимости установлены"

# --- 3. Права ---
$me = "$env:USERDOMAIN\$env:USERNAME"
& icacls $Root /grant "${me}:(OI)(CI)M" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "Не выдались права $me на $Root — файлы, созданные заданиями, придётся править от администратора." }
else { Ok "$me может править файлы, которые создают задания (они идут от SYSTEM)" }

# --- 4. Базы и скрипты ---
foreach ($db in @(Get-ChildItem -LiteralPath $Root -Filter "*.db" -File)) {
    & $venvPy -c "import sqlite3, sys; c = sqlite3.connect(r'$($db.FullName)'); ok = c.execute('PRAGMA integrity_check').fetchone()[0]; print('  $($db.Name):', ok); sys.exit(0 if ok == 'ok' else 1)"
    if ($LASTEXITCODE -ne 0) { Fail "База $($db.Name) повреждена — распакуйте архив заново" }
}
$dirs = @($Root) + @(@("tools\sim", "deploy") | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path $_ })
& $venvPy -m compileall -q -l @dirs | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Скрипты не собираются (compileall) — архив неполный или испорчен" }
& $venvPy -c "import wb_bids, wb_guard, wb_common, wb_collector, yandex_token"
if ($LASTEXITCODE -ne 0) { Fail "Скрипты не загружаются — смотрите сообщение выше" }
Ok "Базы целы, скрипты собираются"
if (-not (Test-Path (Join-Path $Root "crm_config.json"))) { Warn "Нет crm_config.json — скрипты Директа и Метрики попросят токен Яндекса; WB это не мешает." }

# --- 5. Связь с WB и токены (только чтение), затем пробный прогон частой проверки ---
Info "Связь с WB: список кампаний каждого кабинета (только чтение)"
$check = @'
import sys
import wb_bids as B
cabs = B.load_tokens()
bad = 0
for cab, tok in cabs.items():
    try:
        print('  ' + cab + ': кампаний ' + str(len(B.get_campaigns(tok))))
    except B.ApiError as e:
        bad += 1
        print('  ' + cab + ': ОШИБКА ' + str(e))
sys.exit(1 if bad or not cabs else 0)
'@
& $venvPy -c $check
if ($LASTEXITCODE -ne 0) { Fail "Нет связи с WB или токены не подходят (сообщение выше). Расписание не ставлю." }
Ok "С сервера есть связь с WB, токены работают"
Info "Пробный прогон частой проверки лимита (ничего не меняет)"
& $venvPy (Join-Path $Root "wb_guard.py") --limits

# Ручной запуск из PowerShell: «python скрипт.py» — системным Python; ему тоже нужен requests.
$sysReq = & $py -c "import importlib.util, sys; print('venv' if sys.prefix != sys.base_prefix else ('ok' if importlib.util.find_spec('requests') else 'no'))"
if ($sysReq -eq "no") {
    & $py -m pip install --disable-pip-version-check -q --user requests
    if ($LASTEXITCODE -eq 0) { Ok "requests поставлен и для ручного запуска (только для $me, общий Python не тронут)" }
    else { Warn "Для ручного запуска пишите C:\direct\.venv\Scripts\python.exe вместо python" }
} elseif ($sysReq -eq "venv") {
    Warn "python в PATH — чужой venv. Для ручного запуска пишите C:\direct\.venv\Scripts\python.exe вместо python"
}

# --- 6. Расписание ---
if (-not $NoSchedule) {
    $ans = Read-Host "Поставить расписание здесь (автопилот, проверки, сборщик — от SYSTEM, без входа в систему)? да/нет"
    if ($ans -match "^(да|д|yes|y)$") {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "wb_schedule.ps1") -Server
        if ($LASTEXITCODE -ne 0) { Fail "Расписание не поставилось (сообщение выше)" }
        Ok "Расписание стоит"
    } else {
        Warn "Расписание не поставлено. Поставить позже: powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1 -Server (от администратора)"
    }
}

# --- 7. Ярлык дашборда ---
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
$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$shell = New-Object -ComObject WScript.Shell
$lnk = Join-Path $desk "Реклама WB.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force }
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '"' + (Join-Path $Root "wb_dashboard.py") + '"' + $(if ($browser) { ' --browser "' + $browser + '"' } else { "" })
$s.WorkingDirectory = $Root
$icon = Join-Path $Root "wb_dashboard.ico"
if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
$s.Description = "Дашборд рекламы Wildberries: пересобрать и открыть"
$s.Save()
if ($browser) { Ok "Ярлык «Реклама WB» на рабочем столе ($desk), браузер $browser" }
else { Warn "Chrome/Edge не найден — ярлык откроет дашборд браузером по умолчанию (Internet Explorer: графики могут не работать)." }

Write-Host ""
Ok "Готово. Скрипты — в C:\direct, команды те же: cd C:\direct; python <скрипт>.py"
Warn "Удалите архив переноса (C:\temp\direct_move.zip) здесь и на рабочем компьютере — в нём токены."
