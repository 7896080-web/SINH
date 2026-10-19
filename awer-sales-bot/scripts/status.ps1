# Состояние awer-sales-bot: служба, данные 1С, план месяца, бот MAX, копии базы, последние строки журналов.
#   powershell -ExecutionPolicy Bypass -File C:\awer-sales-bot\scripts\status.ps1
# Если что-то не так — пришлите разработчику весь вывод.
param([string]$App = "C:\awer-sales-bot", [string]$Service = "AwerSalesBot", [int]$Lines = 20)

$App = $App.TrimEnd("\")
$py = Join-Path $App ".venv\Scripts\python.exe"
$svc = Get-Service -Name $Service -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "Служба $Service не найдена — запустите scripts\install.ps1" -ForegroundColor Red
} else {
    $color = "Green"
    if ($svc.Status -ne "Running") { $color = "Red" }
    Write-Host ("Служба {0}: {1}" -f $Service, $svc.Status) -ForegroundColor $color
}
foreach ($name in "AwerSalesBot cloud backup", "AwerSalesBot-1C") {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "Задача «$name» не найдена" -ForegroundColor Yellow; continue }
    $info = $task | Get-ScheduledTaskInfo
    Write-Host ("Задача «{0}»: {1}, последний запуск {2}, код 0x{3:X8}, следующий {4}" -f `
        $name, $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
}
$drive = (Get-Item $App).PSDrive
$freeGb = [math]::Round($drive.Free / 1GB, 1)
$color = "Gray"
if ($freeGb -lt 5) { $color = "Red" }
Write-Host ("Свободно на диске {0}: {1} ГБ" -f $drive.Name, $freeGb) -ForegroundColor $color

if (Test-Path $py) {
    Write-Host "`nПроверка (awer-bot doctor):" -ForegroundColor Cyan
    Push-Location $App
    & $py -m awer_sales_bot.cli doctor --backups backups
    Pop-Location
}
foreach ($log in "service.err.log", "cloud_copy.log", "backup.log") {
    $path = Join-Path $App "logs\$log"
    if (Test-Path $path) {
        Write-Host "`n--- logs\$log ---" -ForegroundColor Cyan
        Get-Content $path -Tail $Lines -Encoding UTF8
    }
}
