# Постоянная страница настроек под паролем (ключ Claude API, токены ботов, id пользователей).
#
#   powershell -ExecutionPolicy Bypass -File C:\FinanceBot\deploy\windows\settings.ps1            — включить
#   powershell -ExecutionPolicy Bypass -File C:\FinanceBot\deploy\windows\settings.ps1 -Password  — сменить пароль
#   powershell -ExecutionPolicy Bypass -File C:\FinanceBot\deploy\windows\settings.ps1 -Off       — выключить и закрыть порт
#   ... settings.ps1 -Port 9443   — другой порт
#
# Страницу запускает и останавливает задание «FinanceBot» (супервизор): он
# следит за файлом settings\enabled, который ставит и убирает этот скрипт.
param([switch]$Password, [switch]$Off, [int]$Port = 8765, [string]$App = "C:\FinanceBot")

$ErrorActionPreference = "Continue"
$Dir = Join-Path $App "settings"
$VPy = Join-Path $App "venv\Scripts\python.exe"
$Rule = "FinanceBot settings"

function Fail($text) { Write-Host "`nОшибка: $text" -ForegroundColor Red; exit 1 }
function Stop-Page {
    # Перезапустить страницу (например, после смены пароля): супервизор поднимет её сам.
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'finance\.settings_web\s+serve' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "запустите PowerShell от имени администратора"
}
if (-not (Test-Path $VPy)) { Fail "бот не установлен — сначала deploy\windows\install.ps1" }

if ($Off) {
    Remove-Item (Join-Path $Dir "enabled") -Force -ErrorAction SilentlyContinue
    Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "Страница настроек выключена, порт закрыт."
    exit 0
}

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Push-Location $App
if ($Password -or -not (Test-Path (Join-Path $Dir "password"))) {
    Write-Host ""
    Write-Host "Задайте пароль для страницы настроек (не короче 10 символов)."
    Write-Host "При вводе символы не отображаются — это нормально."
    & $VPy -m finance.settings_web set-password --dir $Dir
    if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "пароль не сохранён" }
    Stop-Page
}
& $VPy -m finance.windows cert $Dir
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Fail "не удалось создать сертификат страницы" }

[IO.File]::WriteAllText((Join-Path $Dir "enabled"), "$Port")
Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $Rule -Direction Inbound -Protocol TCP -LocalPort $Port `
    -Action Allow -Profile Any | Out-Null
Write-Host "Порт $Port открыт в брандмауэре Windows."

if ("$((Get-ScheduledTask -TaskName FinanceBot -ErrorAction SilentlyContinue).State)" -ne "Running") {
    Start-ScheduledTask -TaskName FinanceBot -ErrorAction SilentlyContinue
}
Write-Host "Жду запуска страницы…"
$up = $false
for ($i = 0; $i -lt 30 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    $tcp = New-Object Net.Sockets.TcpClient
    try { $tcp.Connect("127.0.0.1", $Port); $up = $true } catch { } finally { $tcp.Close() }
}
if (-not $up) {
    Get-Content (Join-Path $App "logs\settings.log") -Tail 20 -Encoding UTF8 -ErrorAction SilentlyContinue
    Get-Content (Join-Path $App "logs\supervisor.log") -Tail 10 -Encoding UTF8 -ErrorAction SilentlyContinue
    Fail "страница не запустилась — выше журнал (или порт $Port занят: попробуйте -Port 9443)"
}

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } | ForEach-Object { $_.IPAddress }
Write-Host ""
Write-Host "Страница настроек работает:" -ForegroundColor Green
foreach ($ip in $ips) { Write-Host "   https://${ip}:$Port" }
Write-Host @"

  • Адрес 10.x, 172.16–31.x или 192.168.x — внутренний: снаружи берите внешний
    IP из панели хостинга. Если у хостинга свой файрвол — откройте там порт $Port (TCP).
  • Браузер предупредит о сертификате (он самоподписанный):
    «Дополнительно» → «Перейти на сайт». Соединение шифруется.
  • Вход по паролю. 5 ошибок с одного адреса — блокировка на 15 минут.
  • После сохранения боты сами перезапустятся примерно через 20 секунд.
  • Сменить пароль:  settings.ps1 -Password     Выключить:  settings.ps1 -Off
  • Журнал входов:   $App\logs\settings.log
"@
