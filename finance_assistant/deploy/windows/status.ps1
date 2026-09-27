# Состояние финансового помощника: задания, процессы, последние строки журналов.
#   powershell -ExecutionPolicy Bypass -File C:\FinanceBot\deploy\windows\status.ps1
param([string]$App = "C:\FinanceBot", [int]$Lines = 15)

foreach ($name in "FinanceBot", "FinanceBot-Backup") {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "$name — задание не найдено (запустите install.ps1)" -ForegroundColor Red; continue }
    $info = $task | Get-ScheduledTaskInfo
    Write-Host ("{0,-18} {1,-8} последний запуск: {2}" -f $name, $task.State, $info.LastRunTime)
}
Write-Host ""
Write-Host "Процессы:"
Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '-m\s+finance' } |
    ForEach-Object { Write-Host ("  pid {0,-7} {1}" -f $_.ProcessId, ($_.CommandLine -replace '^.*?-m\s+', '-m ')) }
foreach ($log in "supervisor", "bot", "bot-test", "settings") {
    $path = Join-Path $App "logs\$log.log"
    if (Test-Path $path) {
        Write-Host "`n--- $log.log ---" -ForegroundColor Cyan
        Get-Content $path -Tail $Lines -Encoding UTF8
    }
}
