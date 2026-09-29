<#
.SYNOPSIS
    Предварительная проверка рабочего компьютера перед установкой программы
    маркировки. Ничего не ставит и не меняет — только смотрит и печатает.

.DESCRIPTION
    Проверяет: Python, клиент OpenSSH, КриптоПро CSP и срок лицензии,
    сертификаты с ИНН в личном хранилище, доступ к серверу по 443, True API и
    СУЗ Честного знака. Секретов не печатает: у сертификатов — только владелец,
    ИНН и срок, закрытые ключи не трогаются.

    powershell -ExecutionPolicy Bypass -File C:\marking\deploy\check_workstation.ps1 -Server 136.243.92.95
#>
param(
    [string]$Server = "136.243.92.95",
    [int]$SshPort = 443
)
$ErrorActionPreference = "Continue"
function Good($m) { Write-Host "[OK] $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "[X]  $m" -ForegroundColor Red }
function Note($m) { Write-Host "[?]  $m" -ForegroundColor Yellow }

Write-Host "== Система"
$os = Get-CimInstance Win32_OperatingSystem
Good "$($os.Caption) $($os.Version), пояс: $((Get-TimeZone).Id)"
if ((Get-TimeZone).Id -ne "Russian Standard Time") { Note "пояс не московский — даты УПД и поставок считаются по часам этого компьютера" }

Write-Host "== Python"
$py = $null
foreach ($c in @("python", "py")) {
    try { $v = & $c --version 2>&1; if ($v -match "Python 3\.(1[1-9]|[2-9]\d)") { $py = $c; Good "$c : $v"; break } } catch { }
}
if (-not $py) { Bad "Python 3.11+ не найден (python.org, галочка «Add python.exe to PATH»)" }

Write-Host "== OpenSSH (клиент)"
foreach ($t in @("ssh", "ssh-keygen", "ssh-keyscan", "sftp")) {
    if (Get-Command $t -ErrorAction SilentlyContinue) { Good $t } else { Bad "$t не найден: «Дополнительные компоненты» -> «Клиент OpenSSH»" }
}

Write-Host "== КриптоПро CSP"
$cp = "C:\Program Files\Crypto Pro\CSP"
if (Test-Path "$cp\csptest.exe") {
    Good "CSP установлен: $cp"
    if (Test-Path "$cp\cpconfig.exe") {
        $lic = & "$cp\cpconfig.exe" -license -view 2>&1 | Out-String
        Write-Host ($lic.Trim())
    }
} else { Bad "КриптоПро CSP не найден в $cp" }

Write-Host "== Сертификаты с ИНН (личное хранилище текущего пользователя)"
$certs = Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue
$found = 0
foreach ($c in $certs) {
    # ИНН физлица/ИП: OID 1.2.643.3.131.1.1; в строке субъекта — «ИНН=» или «OID.1.2.643.3.131.1.1=».
    if ($c.Subject -match "(ИНН|INN|OID\.1\.2\.643\.3\.131\.1\.1)=(\d{10,12})") {
        $inn = $Matches[2]
        $cn = if ($c.Subject -match "CN=([^,]+)") { $Matches[1] } else { "?" }
        $state = if ($c.NotAfter -lt (Get-Date)) { "ИСТЁК" } else { "до $($c.NotAfter.ToString('dd.MM.yyyy'))" }
        Write-Host ("   {0} | ИНН {1} | {2} | ключ {3}" -f $cn, $inn, $state, $(if ($c.HasPrivateKey) { "есть" } else { "НЕТ" }))
        $found++
    }
}
if ($found) { Good "сертификатов с ИНН: $found" } else { Bad "сертификатов с ИНН нет в Cert:\CurrentUser\My" }

Write-Host "== Сеть"
$t = Test-NetConnection $Server -Port $SshPort -WarningAction SilentlyContinue
if ($t.TcpTestSucceeded) { Good "сервер $Server`:$SshPort доступен" } else { Bad "сервер $Server`:$SshPort недоступен" }
function Probe($name, $url) {
    try {
        $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 15
        Good "$name : HTTP $($r.StatusCode)"
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 451) { Bad "$name : HTTP 451 — адрес заблокирован для этой сети" }
        elseif ($code) { Good "$name : HTTP $code (сервер отвечает; 401/400 без входа — нормально)" }
        else { Bad "$name : нет ответа ($($_.Exception.Message))" }
    }
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Probe "True API auth/key" "https://markirovka.crpt.ru/api/v3/true-api/auth/key"
Probe "СУЗ ping" "https://suzgrid.crpt.ru/api/v3/ping?omsId=00000000-0000-0000-0000-000000000000"

Write-Host "== Ключ сервера (если сервер отвечает)"
$scan = & ssh-keyscan -p $SshPort -t ed25519 $Server 2>$null
if ($scan) {
    $tmp = New-TemporaryFile
    Set-Content $tmp $scan -Encoding ascii
    & ssh-keygen -l -f $tmp
    Remove-Item $tmp
    Note "этот отпечаток сверяют с сервером (SFTP_1C.md, шаг 5) — сам по себе он ничего не доказывает"
} else { Note "SSH на $Server`:$SshPort не ответил" }
