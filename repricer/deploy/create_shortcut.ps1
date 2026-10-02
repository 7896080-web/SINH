<#
.SYNOPSIS
    Ярлык «Репрайсер» на рабочем столе — со своим значком и без мигающего окна.

.DESCRIPTION
    Значок — deploy\repricer.ico (ценник; рисует scripts\gen_icon.py). Путь к нему
    ярлык запоминает АБСОЛЮТНЫЙ: удалят файл — значок пропадёт, ближайшее
    обновление положит его обратно и пересоздаст ярлык.

    Ярлык запускает deploy\launch_repricer.vbs, а не powershell напрямую:
    powershell из ярлыка на каждом щелчке на секунду показывает синее окно даже
    со скрытым стилем. Сам запуск делает run_repricer.ps1: программа уже работает —
    просто открывает браузер, иначе поднимает её и ждёт ответа.

    Скрипт идемпотентен: существующий ярлык перезаписывается. Его зовут и
    install_workstation.ps1, и update_workstation.ps1.

    powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\create_shortcut.ps1
    powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\create_shortcut.ps1 -Autostart

.PARAMETER Port
    Порт программы (по умолчанию 8002).
.PARAMETER Autostart
    Ещё и ярлык в автозагрузке: программа поднимется при входе в Windows.
.PARAMETER RefreshOnly
    Только обновить уже существующие ярлыки (так зовёт обновление): не
    заводить ярлык там, где человек его удалил.
#>
[CmdletBinding()]
param([int]$Port = 8002, [switch]$Autostart, [switch]$RefreshOnly)
$ErrorActionPreference = "Stop"

$Root = Split-Path $PSScriptRoot -Parent
$vbs = Join-Path $Root "deploy\launch_repricer.vbs"
$icon = Join-Path $Root "deploy\repricer.ico"
foreach ($f in @($vbs, $icon, (Join-Path $Root "deploy\run_repricer.ps1"))) {
    if (-not (Test-Path $f)) { Write-Host "[X] Нет файла $f" -ForegroundColor Red; exit 1 }
}

$shell = New-Object -ComObject WScript.Shell
function Set-Shortcut($path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
    $s.Arguments = "`"$vbs`" $Port"
    $s.WorkingDirectory = $Root
    $s.IconLocation = "$icon,0"
    $s.Description = "Репрайсер — цены на Wildberries, Ozon, Яндекс KIT и Lamoda"
    $s.Save()
    Write-Host "[OK] Ярлык: $path" -ForegroundColor Green
}

$targets = @(Join-Path ([Environment]::GetFolderPath("Desktop")) "Репрайсер.lnk")
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Репрайсер.lnk"
if ($Autostart -or ($RefreshOnly -and (Test-Path $startup))) { $targets += $startup }
foreach ($t in $targets) {
    if ($RefreshOnly -and -not (Test-Path $t)) { continue }
    Set-Shortcut $t
}
