<#
.SYNOPSIS
    Сервер: ярлык «Реклама WB» на рабочем столе — дашборд и страница «Бюджеты» (wb_web.py).

.DESCRIPTION
    Браузер указывается ЯВНО (Chrome, Edge, Яндекс, Firefox): на Windows Server браузер по умолчанию — Internet
    Explorer, в нём графики дашборда и страница бюджетов не работают. Ярлыки — на общем рабочем столе, если туда
    можно писать (проверяется записью пробного файла), иначе — на своём. Значки разной формы, чтобы соседние ярлыки
    различались и по RDP. Вызывается из install_server.ps1 и update_server.ps1; можно и отдельно:
        powershell -ExecutionPolicy Bypass -File C:\direct\deploy\shortcuts.ps1
#>
[CmdletBinding()]
param([string]$Root = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = "Stop"
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

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
try {
    $probe = Join-Path $desk ("probe_" + [guid]::NewGuid().ToString("N") + ".tmp")
    Set-Content -Path $probe -Value "" -ErrorAction Stop
    Remove-Item $probe -Force
} catch {
    $desk = [Environment]::GetFolderPath("Desktop")
}
$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { Warn "Нет $pyw — сначала install_server.ps1"; exit 1 }
$shell = New-Object -ComObject WScript.Shell

function Make-Link($title, $script, $iconName, $desc) {
    if (-not (Test-Path (Join-Path $Root $script))) { return }
    $lnk = Join-Path $desk "$title.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $Root $script) + '"' + $(if ($browser) { ' --browser "' + $browser + '"' } else { "" })
    $s.WorkingDirectory = $Root
    $icon = Join-Path $Root $iconName
    if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
    $s.Description = $desc
    $s.Save()
    Ok "Ярлык «$title» ($desk)"
}

# «Реклама WB» — дашборд и страница «Бюджеты» (wb_web.py); без wb_web.py — как раньше, файл дашборда
if (Test-Path (Join-Path $Root "wb_web.py")) {
    Make-Link "Реклама WB" "wb_web.py" "wb_dashboard.ico" "Реклама Wildberries: дашборд и бюджеты кампаний"
} else {
    Make-Link "Реклама WB" "wb_dashboard.py" "wb_dashboard.ico" "Дашборд рекламы Wildberries: пересобрать и открыть"
}
# отдельного ярлыка «Бюджеты WB» нет (решение пользователя 16.10.2026: страница внутри «Реклама WB»)
$old = Join-Path $desk "Бюджеты WB.lnk"
if (Test-Path $old) { Remove-Item $old -Force }
if ($browser) { Ok "Браузер ярлыков: $browser" }
else { Warn "Chrome/Edge/Яндекс не найден — ярлыки откроют браузер по умолчанию (Internet Explorer: страницы могут не работать)." }
