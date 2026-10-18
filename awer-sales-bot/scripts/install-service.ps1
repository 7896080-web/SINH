# Установка awer-sales-bot как службы Windows через NSSM (https://nssm.cc/download).
# Запуск: PowerShell от администратора, из папки проекта:  .\scripts\install-service.ps1
param(
    [string]$Nssm = "C:\nssm\win64\nssm.exe",
    [string]$Name = "AwerSalesBot"
)
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\awer-bot.exe"
if (!(Test-Path $exe)) { throw "Не найден $exe — сначала pip install -e ." }
& $Nssm install $Name $exe "serve" "--host" "127.0.0.1" "--port" "8080"
& $Nssm set $Name AppDirectory $root
& $Nssm set $Name AppStdout (Join-Path $root "data\service.log")
& $Nssm set $Name AppStderr (Join-Path $root "data\service.log")
& $Nssm set $Name AppRotateFiles 1
& $Nssm set $Name AppRotateBytes 5000000
& $Nssm set $Name Start SERVICE_AUTO_START
# поток бота упал — программа завершается сама (bot.guarded); NSSM поднимает её через 10 секунд
& $Nssm set $Name AppExit Default Restart
& $Nssm set $Name AppRestartDelay 10000
& $Nssm start $Name
Write-Host "Служба $Name запущена. Админка: http://127.0.0.1:8080"
