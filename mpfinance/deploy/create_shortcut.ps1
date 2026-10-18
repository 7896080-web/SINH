<#
.SYNOPSIS
    Ярлык «Финансы площадок» на общем рабочем столе — со своим значком.

.DESCRIPTION
    Зовут install_server.ps1 и update_server.ps1; можно запустить и отдельно:
        powershell -ExecutionPolicy Bypass -File C:\mpfinance\deploy\create_shortcut.ps1

    Браузер записывается в ярлык ЯВНО (Chrome, Edge, Яндекс): браузер по
    умолчанию на Windows Server 2016 — Internet Explorer, и страницы программы
    на нём не работают (шкалы прогресса, отправка форм фоном).

    Значок — deploy\mpfinance.ico (круговая диаграмма; рисует его
    scripts\gen_icon.py). Путь в ярлыке абсолютный: удалят файл — значок
    пропадёт, ближайший накат положит его обратно.

    Право писать на общий рабочий стол проверяется ЗАПИСЬЮ пробного файла, а не
    наличием папки: без прав администратора папка есть, а запись падает, и ярлык
    «создался» бы только в выводе скрипта. Нет прав — ярлык кладётся на рабочий
    стол текущего пользователя, и об этом сказано.
#>
[CmdletBinding()]
param([int]$WebPort = 8003)
$ErrorActionPreference = "Stop"
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

$Root = Split-Path $PSScriptRoot -Parent
$url = "http://127.0.0.1:$WebPort"
$icon = Join-Path $Root "deploy\mpfinance.ico"

$browser = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Yandex\YandexBrowser\Application\browser.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $browser) {
    Warn "Chrome/Edge не найден — ярлык не создан. Откройте $url в Chrome вручную (Internet Explorer не подойдёт)."
    exit 0
}

function Writable($dir) {
    if (-not (Test-Path $dir)) { return $false }
    $probe = Join-Path $dir (".mpfinance-probe-" + [guid]::NewGuid().ToString("N"))
    try { Set-Content -Path $probe -Value "x" -ErrorAction Stop; Remove-Item $probe -Force; return $true }
    catch { return $false }
}

$desk = Join-Path $env:PUBLIC "Desktop"
if (-not (Writable $desk)) {
    $own = [Environment]::GetFolderPath("Desktop")
    Warn "Нет прав писать на общий рабочий стол ($desk) — ярлык будет только у пользователя $env:USERNAME."
    $desk = $own
}

$lnk = Join-Path $desk "Финансы площадок.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force }
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $browser
$s.Arguments = '"' + $url + '"'
$s.WorkingDirectory = Split-Path $browser
$s.Description = "Финансы площадок — $url"
if (Test-Path $icon) {
    $s.IconLocation = "$icon,0"
} else {
    $s.IconLocation = "$browser,0"
    Warn "Нет файла значка $icon — у ярлыка значок браузера."
}
$s.Save()
Ok "Ярлык «Финансы площадок» на рабочем столе ($desk), значок: $(if (Test-Path $icon) { 'свой' } else { 'браузера' })"
