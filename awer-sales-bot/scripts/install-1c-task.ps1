# Ежедневная выгрузка из 1С для бота — задача Планировщика заданий Windows (docs/1C.md, аудит 1С-3).
# Запуск: PowerShell от администратора, из папки проекта:
#   .\scripts\install-1c-task.ps1 -Base "localhost\<имя базы>"
# Имя базы — как в списке баз 1С (сервер\база). Скрипт спросит учётную запись Windows, под которой запускать 1С:
# её пароль хранит Windows, в командной строке задачи его нет.
# Вход в 1С — по аутентификации Windows (/WA+): в Конфигураторе у пользователя 1С «Бот» включите
# «Аутентификация операционной системы» и укажите эту учётную запись Windows.
param(
    [Parameter(Mandatory = $true)][string]$Base,
    [string]$OneC = "C:\Program Files\1cv8\8.3.9.2233\bin\1cv8.exe",
    [string]$Time = "07:30",          # раньше утренней рассылки бота (schedule.daily_send_time, 09:30)
    [string]$Name = "AwerSalesBot-1C"
)
$root = Split-Path -Parent $PSScriptRoot
$epf = Join-Path $root "onec\ВыгрузкаПродажДляБота.epf"
if (!(Test-Path $OneC)) { throw "Не найден $OneC — укажите путь к 1cv8.exe параметром -OneC" }
if (!(Test-Path $epf)) { throw "Не найден $epf — сначала соберите обработку (docs/1C.md, «Сборка в Конфигураторе»)" }

# /S — клиент-серверная база; /C"AUTO" латиницей — кириллица портится в задачах Планировщика;
# /DisableStartupDialogs — без него при нехватке лицензий 1С ждёт ответа человека вечно
$arguments = "ENTERPRISE /S`"$Base`" /WA+ /Execute`"$epf`" /C`"AUTO`" /DisableStartupMessages /DisableStartupDialogs"
$action = New-ScheduledTaskAction -Execute $OneC -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# не дольше 30 минут (зависшая 1С держит лицензию), пропущенный запуск — при включении, без параллельных копий
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" `
    -Message "Учётная запись Windows для запуска 1С (та, что связана с пользователем 1С «Бот»)"
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password `
    -Description "Выгрузка продаж, остатков и поступлений для awer-sales-bot" -Force | Out-Null
Write-Host "Задача $Name создана: каждый день в $Time."
Write-Host "Проверка: Планировщик заданий → $Name → «Выполнить»; через пару минут файлы появятся в data\inbox."
