# Состояние финансового помощника: задания, процессы, последние строки журналов.
#   powershell -ExecutionPolicy Bypass -File C:\FinanceBot\deploy\windows\status.ps1
param([string]$App = "C:\FinanceBot", [int]$Lines = 15)

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Запустите PowerShell от имени администратора — иначе журналы и процессы не видны." -ForegroundColor Red
    exit 1
}

foreach ($name in "FinanceBot", "FinanceBot-Backup") {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "$name — задание не найдено (запустите install.ps1)" -ForegroundColor Red; continue }
    $info = $task | Get-ScheduledTaskInfo
    $code = "0x{0:X8}" -f $info.LastTaskResult
    $note = ""
    if ($task.State -eq "Running" -and $code -eq "0x800710E0") {
        $note = " (проверка раз в 5 минут: уже работает — это нормально)"
    }
    Write-Host ("{0,-18} {1,-8} последний запуск: {2}  код: {3}{4}" -f $name, $task.State, $info.LastRunTime, $code, $note)
}
Write-Host ""
Write-Host "Процессы:"
# Каждый процесс виден дважды: посредник venv (venv\Scripts\python.exe) и сам
# Python. Показываем только сам Python.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '-m\s+finance' -and $_.ExecutablePath -notlike "$App\venv\*" } |
    ForEach-Object { Write-Host ("  pid {0,-7} {1}" -f $_.ProcessId, ($_.CommandLine -replace '^.*?-m\s+', '-m ')) }
foreach ($log in "supervisor", "bot", "bot-test", "settings") {
    $path = Join-Path $App "logs\$log.log"
    if (Test-Path $path) {
        Write-Host "`n--- $log.log ---" -ForegroundColor Cyan
        Get-Content $path -Tail $Lines -Encoding UTF8
    }
}
