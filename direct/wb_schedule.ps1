# Расписание автопилота WB в Планировщике заданий Windows (запускать ПОСЛЕ первого удачного ручного
# python wb_bids.py --apply). Решение пользователя 12.10.2026:
#   • wb_bids.py --apply — 3 раза в день: 08:00, 13:00, 18:00;
#   • wb_guard.py --apply — каждый час в :40 с 07:40 до 23:40: ставка за рекомендацией WB и минимумом, не выше
#     окупаемого потолка, вниз и вверх сразу (≈10 минут).
#   • wb_positions.py — каждый день в 06:00 (позиции, воронка, реклама по дням → wb_history.db; ≈1 час на 3 кабинета;
#     только чтение), чтобы к автопилоту в 8:00 и к утру дашборд был свежим (решение 14.10.2026);
#   • после каждого запуска — пересборка дашборда wb_dashboard.html (python wb_dashboard.py --no-open), 13.10.2026;
#   • wb_collector.py — каждые 15 минут с 07:00 до 24:00: рекомендованные ставки и размеры в базу wb_rec.db
#     (только чтение WB; решение 15.10.2026). Дашборд после него не пересобирается.
#   • wb_guard.py --limits --apply — каждые 5 минут с 07:02 до 24:00: свой дневной лимит кампаний (показы; клики с лимитом
#     ниже 1 000 ₽) — пауза до утра, пауза «по графику» и запуск, когда спрос по часам догнал (решение 15.10.2026).
# С 14.10.2026 задания запускают pythonw.exe wb_run.py <скрипт> (без окна; своё ограничение по времени, после
# которого зависший скрипт останавливается и снимается его блокировка; журналы > 5 МБ переносятся в *.old.txt).
# Работают и от батареи ноутбука. Переустановить после обновления: ту же команду «Установить».
# Установить:  powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1
# Проверить:   Get-ScheduledTask -TaskName "AWER_WB_*"
# Снять:       powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1 -Remove
# Задания работают, пока компьютер включён и вы вошли в Windows (окно не появляется). Вывод — в C:\direct\wb_sched_out.txt,
# подробности — в wb_bids_log.txt и wb_guard_log.txt.
param([switch]$Remove)

$dir = "C:\direct"
$names = @("AWER_WB_bids", "AWER_WB_guard", "AWER_WB_positions", "AWER_WB_collector", "AWER_WB_limits")

if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Задания расписания WB сняты."
    exit
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host "Не найден python. Установите Python или проверьте, что команда python работает."; exit 1 }
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
if (-not (Test-Path (Join-Path $dir "wb_run.py"))) { Write-Host "Нет $dir\wb_run.py — сначала скопируйте новые файлы (git pull + Copy-Item)."; exit 1 }

function Make-Action($script, $extra = "--apply", $minutes = 55) {
    # wb_run.py: запуск без окна, остановка зависшего скрипта через $minutes мин, затем пересборка дашборда
    $arg = "wb_run.py $script $extra --timeout $minutes"
    New-ScheduledTaskAction -Execute $pyw -Argument $arg -WorkingDirectory $dir
}

function Make-Settings($limit) {
    New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit $limit `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
}

$set = Make-Settings (New-TimeSpan -Hours 1)

$bidsTriggers = @("08:00", "13:00", "18:00") | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
Register-ScheduledTask -TaskName "AWER_WB_bids" -Action (Make-Action "wb_bids.py" "--apply" 55) -Trigger $bidsTriggers `
    -Settings $set -Description "Автопилот ставок WB (wb_bids.py --apply), 3 раза в день" -Force | Out-Null

$guardTrigger = New-ScheduledTaskTrigger -Daily -At "07:40"
$rep = New-ScheduledTaskTrigger -Once -At "07:40" -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 16)
$guardTrigger.Repetition = $rep.Repetition
$guardSet = Make-Settings (New-TimeSpan -Minutes 25)
Register-ScheduledTask -TaskName "AWER_WB_guard" -Action (Make-Action "wb_guard.py" "--apply" 22) -Trigger $guardTrigger `
    -Settings $guardSet -Description "Часовая проверка ставок WB (wb_guard.py --apply)" -Force | Out-Null

$posSet = Make-Settings (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "AWER_WB_positions" -Action (Make-Action "wb_positions.py" "" 110) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "06:00") -Settings $posSet `
    -Description "Позиции и воронка WB (wb_positions.py, только чтение), каждый день в 6:00" -Force | Out-Null

if (Test-Path (Join-Path $dir "wb_collector.py")) {
    $colTrigger = New-ScheduledTaskTrigger -Daily -At "07:00"
    $colRep = New-ScheduledTaskTrigger -Once -At "07:00" -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 17)
    $colTrigger.Repetition = $colRep.Repetition
    $colSet = Make-Settings (New-TimeSpan -Minutes 16)
    Register-ScheduledTask -TaskName "AWER_WB_collector" -Action (Make-Action "wb_collector.py" "" 14) -Trigger $colTrigger `
        -Settings $colSet -Description "Сборщик рекомендаций и размеров WB (wb_collector.py, только чтение), каждые 15 мин" -Force | Out-Null
}

$limTrigger = New-ScheduledTaskTrigger -Daily -At "07:02"
$limRep = New-ScheduledTaskTrigger -Once -At "07:02" -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 17)
$limTrigger.Repetition = $limRep.Repetition
$limSet = Make-Settings (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "AWER_WB_limits" -Action (Make-Action "wb_guard.py" "--limits --apply" 4) -Trigger $limTrigger `
    -Settings $limSet -Description "Дневной лимит кампаний WB каждые 5 мин (wb_guard.py --limits --apply)" -Force | Out-Null

Write-Host "Готово. Python: $pyw (без окна, через wb_run.py)"
Get-ScheduledTask -TaskName "AWER_WB_*" | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    Write-Host ("  {0}: следующий запуск {1}" -f $_.TaskName, $i.NextRunTime)
}
