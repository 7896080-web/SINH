# Ежедневная выгрузка из 1С для бота — задача Планировщика заданий Windows (docs/1C.md, аудит 1С-3).
# Запуск: PowerShell от администратора, из папки проекта, под той учётной записью Windows, которая обычно
# вошла на сервере (как у Sync Admin: толстому клиенту 1С нужна интерактивная сессия, в «фоне» без входа он
# не работает):
#   .\scripts\install-1c-task.ps1 -Base "localhost\<имя базы>"
# Вход в 1С — по аутентификации Windows (/WA+): в Конфигураторе у пользователя 1С «Бот» включите
# «Аутентификация операционной системы» и укажите эту учётную запись Windows. Тогда пароль нигде не хранится.
# Если так нельзя — -OneCUser "Бот": скрипт спросит пароль 1С, и он будет виден в свойствах задачи (как у Sync Admin).
param(
    [Parameter(Mandatory = $true)][string]$Base,
    [string]$OneC = "C:\Program Files\1cv8\8.3.9.2233\bin\1cv8.exe",
    [string]$Time = "07:30",          # раньше утренней рассылки бота (schedule.daily_send_time, 09:30) и открытия магазинов
    [string]$Retry = "08:30",         # повтор: если в 07:30 не хватило лицензии 1С; выгрузка дня просто заменится
    [string]$OneCUser = "",
    [string]$Name = "AwerSalesBot-1C"
)
$root = Split-Path -Parent $PSScriptRoot
$epf = Join-Path $root "onec\ВыгрузкаПродажДляБота.epf"
if (!(Test-Path $OneC)) { throw "Не найден $OneC — укажите путь к 1cv8.exe параметром -OneC" }
if (!(Test-Path $epf)) { throw "Не найден $epf — сначала соберите обработку (docs/1C.md, «Сборка в Конфигураторе»)" }

$auth = "/WA+"
if ($OneCUser) {
    $sec = Read-Host -AsSecureString "Пароль пользователя 1С «$OneCUser»"
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    $auth = "/WA- /N`"$OneCUser`" /P`"$plain`""
}
# /S — клиент-серверная база; /CAUTO латиницей и без кавычек (как /CEXCHANGE у Sync Admin);
# /DisableStartupDialogs — без него при нехватке лицензий 1С ждёт ответа человека вечно
$arguments = "ENTERPRISE /S`"$Base`" $auth /Execute`"$epf`" /CAUTO /DisableStartupDialogs /DisableStartupMessages"
$action = New-ScheduledTaskAction -Execute $OneC -Argument $arguments
$triggers = @(New-ScheduledTaskTrigger -Daily -At $Time)
if ($Retry) { $triggers += New-ScheduledTaskTrigger -Daily -At $Retry }
# обычно 1–2 минуты; дольше 15 — 1С зависла и держит лицензию (её нужна и Sync Admin) — снять;
# пропущенный запуск — когда пользователь войдёт; вторая копия не запускается
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable `
    -MultipleInstances IgnoreNew
# «Выполнять только для вошедшего пользователя» — интерактивная сессия, пароль Windows не нужен
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Description "Выгрузка продаж, остатков и поступлений для awer-sales-bot" -Force | Out-Null
Write-Host "Задача $Name создана: каждый день в $Time и $Retry, в сессии $env:USERNAME (на сервере он должен быть вошедшим)."
Write-Host "Проверка: Планировщик заданий → $Name → «Выполнить»; через пару минут файлы появятся в data\inbox."
