# Расписание автопилота WB в Планировщике заданий Windows (запускать ПОСЛЕ первого удачного ручного
# python wb_bids.py --apply). Решение пользователя 12.10.2026:
#   • wb_bids.py --apply — 3 раза в день: 08:00, 13:00, 18:00;
#   • wb_guard.py --apply — каждый час в :40 с 07:40 до 23:40: ставка за рекомендацией WB и минимумом, не выше
#     окупаемого потолка, вниз и вверх сразу (≈10 минут).
#   • wb_positions.py — каждый день в 06:00 (позиции, воронка, реклама по дням → wb_history.db; ≈1 час на 3 кабинета;
#     только чтение), чтобы к автопилоту в 8:00 и к утру дашборд был свежим (решение 14.10.2026);
#   • после каждого запуска — пересборка дашборда wb_dashboard.html (python wb_dashboard.py --no-open), 13.10.2026.
# Установить:  powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1
# Проверить:   Get-ScheduledTask -TaskName "AWER_WB_*"
# Снять:       powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1 -Remove
# Задания работают, пока компьютер включён и вы вошли в Windows. Вывод — в C:\direct\wb_sched_out.txt,
# подробности — в wb_bids_log.txt и wb_guard_log.txt.
param([switch]$Remove)

$dir = "C:\direct"
$names = @("AWER_WB_bids", "AWER_WB_guard", "AWER_WB_positions")

if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Задания расписания WB сняты."
    exit
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host "Не найден python. Установите Python или проверьте, что команда python работает."; exit 1 }

function Make-Action($script, $extra = "--apply") {
    # после скрипта (даже если он упал) пересобрать дашборд — страница всегда показывает последнее состояние
    $cmd = "/c set PYTHONIOENCODING=utf-8&& cd /d $dir && `"$py`" $script $extra >> wb_sched_out.txt 2>&1 & `"$py`" wb_dashboard.py --no-open >> wb_sched_out.txt 2>&1"
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $dir
}

$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$bidsTriggers = @("08:00", "13:00", "18:00") | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
Register-ScheduledTask -TaskName "AWER_WB_bids" -Action (Make-Action "wb_bids.py") -Trigger $bidsTriggers `
    -Settings $set -Description "Автопилот ставок WB (wb_bids.py --apply), 3 раза в день" -Force | Out-Null

$guardTrigger = New-ScheduledTaskTrigger -Daily -At "07:40"
$rep = New-ScheduledTaskTrigger -Once -At "07:40" -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 16)
$guardTrigger.Repetition = $rep.Repetition
$guardSet = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
Register-ScheduledTask -TaskName "AWER_WB_guard" -Action (Make-Action "wb_guard.py") -Trigger $guardTrigger `
    -Settings $guardSet -Description "Часовая проверка ставок WB (wb_guard.py --apply)" -Force | Out-Null

$posSet = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "AWER_WB_positions" -Action (Make-Action "wb_positions.py" "") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "06:00") -Settings $posSet `
    -Description "Позиции и воронка WB (wb_positions.py, только чтение), каждый день в 6:00" -Force | Out-Null

Write-Host "Готово. Python: $py"
Get-ScheduledTask -TaskName "AWER_WB_*" | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    Write-Host ("  {0}: следующий запуск {1}" -f $_.TaskName, $i.NextRunTime)
}
