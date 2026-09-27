# Остановить финансового помощника (оба бота и страницу настроек) до
# перезагрузки или до Start-ScheduledTask FinanceBot.
#   powershell -ExecutionPolicy Bypass -File C:\FinanceBot\deploy\windows\stop.ps1
# Задание при остановке само не гасит запущенные им процессы — поэтому скрипт.
# Задание при этом отключается, иначе через 5 минут оно запустило бы бота снова.
param([switch]$Start)

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Запустите PowerShell от имени администратора." -ForegroundColor Red
    exit 1
}
if ($Start) {
    Enable-ScheduledTask -TaskName FinanceBot | Out-Null
    Start-ScheduledTask -TaskName FinanceBot
    Write-Host "Запущено."
    exit 0
}
Disable-ScheduledTask -TaskName FinanceBot | Out-Null
Stop-ScheduledTask -TaskName FinanceBot -ErrorAction SilentlyContinue
for ($i = 0; $i -lt 15; $i++) {
    $left = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '-m\s+finance(\.supervise|\.settings_web)?(\s|$)' -and
                       $_.CommandLine -notmatch 'set-password' })
    if (-not $left) { Write-Host "Остановлено. Запустить снова: stop.ps1 -Start"; exit 0 }
    $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}
Write-Host "Не все процессы остановились — перезагрузите сервер." -ForegroundColor Red
exit 1
