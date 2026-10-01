<#
.SYNOPSIS
    Настройка сервера для программы маркировки: SFTP-доступ к C:\sync для
    рабочего компьютера с КриптоПро. Больше на сервер ничего не ставится.

.DESCRIPTION
    Делает то же, что deploy\SFTP_1C.md, шаги 2-5, но без ручной правки файлов:
      1. проверяет, что OpenSSH Server установлен, а порт 443 свободен или
         уже занят им самим;
      2. заводит учётную запись marking_sftp (без прав администратора и RDP,
         пароль случайный и нигде не показывается — вход только по ключу);
      3. создаёт C:\sync\results\marking и C:\sync\archive\marking и даёт
         marking_sftp право менять ТОЛЬКО tasks, results\marking, archive\marking;
      4. правит sshd_config (копия прежнего — рядом): порт 443, пароли
         выключены, AllowUsers, блок Match User marking_sftp с ChrootDirectory
         C:\sync и ForceCommand internal-sftp;
      5. кладёт открытый ключ рабочего компьютера с правами SYSTEM+Администраторы;
      6. проверяет конфиг (sshd -t; ошибка — прежний файл возвращается),
         открывает 443 в брандмауэре, перезапускает sshd;
      7. печатает отпечаток ключа сервера — его сверяют с рабочим компьютером.

    Повторный запуск безопасен. sync_admin, 1С и их службы не трогает.

.NOTES
    PowerShell от администратора. Открытый ключ — строка ssh-ed25519 ... из
    вывода install_workstation.ps1 на рабочем компьютере (файл id_ed25519.pub):
        powershell -ExecutionPolicy Bypass -File C:\marking_setup\server_sftp_setup.ps1 -PublicKey "ssh-ed25519 AAAA... marking@PC"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicKey,
    [string]$SyncRoot = "C:\sync",
    [int]$Port = 443,
    [string]$User = "marking_sftp",
    # Внешний адрес (или подсеть) рабочего компьютера с КриптоПро, например
    # 203.0.113.7 или 203.0.113.0/24. Задан — SSH на этом порту пускает только
    # его; не задан — весь интернет (вход всё равно только по ключу).
    [string]$AllowFrom = ""
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Запустите PowerShell от администратора." }

# --- 0. Ключ: только открытая часть ---
$PublicKey = $PublicKey.Trim()
if ($PublicKey -match "PRIVATE") { Fail "Это ЗАКРЫТЫЙ ключ. Нужна строка из id_ed25519.pub, а закрытый ключ с рабочего компьютера не выносится." }
if ($PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( [^\r\n]*)?$') { Fail "Ожидается одна строка вида «ssh-ed25519 AAAA... комментарий»." }

# --- 1. OpenSSH Server и порт ---
$sshDir = "C:\Program Files\OpenSSH"
$sshd = Join-Path $sshDir "sshd.exe"
if (-not (Test-Path $sshd) -or -not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    Fail ("OpenSSH Server не установлен. Скачайте OpenSSH-Win64-v*.msi (не Preview) со страницы " +
          "github.com/PowerShell/Win32-OpenSSH/releases и выполните: msiexec /i <файл> ADDLOCAL=Server — затем запустите этот скрипт снова.")
}
Ok "OpenSSH Server: $sshd"
$cfgDir = "C:\ProgramData\ssh"
$cfg = Join-Path $cfgDir "sshd_config"
if (-not (Test-Path $cfg)) {
    Info "Первый запуск sshd — создаст sshd_config и ключи сервера"
    Start-Service sshd; Start-Sleep -Seconds 2; Stop-Service sshd
}
if (-not (Test-Path $cfg)) { Fail "Нет $cfg после первого запуска sshd." }

$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($l in $listen) {
    $p = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -ne "sshd") {
        Fail "Порт $Port занят процессом $($p.ProcessName) (PID $($p.Id)). SSH на него не встанет — разберитесь, что это, прежде чем продолжать."
    }
}
Ok "Порт $Port свободен или уже у sshd"

# --- 2. Учётная запись ---
if (-not (Get-LocalUser -Name $User -ErrorAction SilentlyContinue)) {
    Add-Type -AssemblyName System.Web
    $pw = ConvertTo-SecureString ([System.Web.Security.Membership]::GeneratePassword(32, 6)) -AsPlainText -Force
    New-LocalUser -Name $User -Password $pw -PasswordNeverExpires -UserMayNotChangePassword `
        -Description "SFTP обмена программы маркировки с 1С" | Out-Null
    Ok "Учётная запись $User создана (пароль случайный, для SSH не используется)"
} else {
    Ok "Учётная запись $User уже есть"
}
foreach ($g in @("Administrators", "Администраторы", "Remote Desktop Users", "Пользователи удаленного рабочего стола")) {
    if (Get-LocalGroupMember -Group $g -Member $User -ErrorAction SilentlyContinue) {
        Fail "$User состоит в группе «$g» — так быть не должно. Уберите и запустите снова."
    }
}

# --- 3. Папки и права ---
if (-not (Test-Path "$SyncRoot\tasks")) { Fail "Нет $SyncRoot\tasks — это папка обмена sync_admin, она должна уже быть." }
foreach ($d in @("$SyncRoot\results\marking", "$SyncRoot\archive\marking")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
foreach ($d in @("$SyncRoot\tasks", "$SyncRoot\results\marking", "$SyncRoot\archive\marking")) {
    & icacls $d /grant "${User}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "icacls $d не прошёл" }
}
Ok "Права: $User меняет только tasks, results\marking, archive\marking"

# --- 4. sshd_config ---
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "$cfg.before-marking-$stamp"
Copy-Item $cfg $backup
$lines = [System.Collections.Generic.List[string]](Get-Content $cfg)
$firstMatch = -1
for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match '^\s*Match\s') { $firstMatch = $i; break } }
$globalEnd = if ($firstMatch -ge 0) { $firstMatch } else { $lines.Count }

function Set-Global($name, $value) {
    # Глобальная директива обязана стоять ДО первого Match: после него она
    # относилась бы только к тому блоку.
    for ($i = 0; $i -lt $script:globalEnd; $i++) {
        if ($script:lines[$i] -match "^\s*#?\s*$name\s") { $script:lines[$i] = "$name $value"; return }
    }
    $script:lines.Insert($script:globalEnd, "$name $value"); $script:globalEnd++
}
Set-Global "Port" $Port
Set-Global "PasswordAuthentication" "no"

$allowIdx = -1
for ($i = 0; $i -lt $globalEnd; $i++) { if ($lines[$i] -match '^\s*AllowUsers\s') { $allowIdx = $i; break } }
if ($allowIdx -ge 0) {
    if ($lines[$allowIdx] -notmatch "(\s|^)$User(\s|$)") { $lines[$allowIdx] = $lines[$allowIdx].TrimEnd() + " $User" }
} else {
    # AllowUsers впервые: SSH на этом сервере пускает ТОЛЬКО перечисленных.
    $lines.Insert($globalEnd, "AllowUsers $User"); $globalEnd++
    Warn "Добавлено «AllowUsers $User»: других пользователей SSH на этом сервере больше не пускает."
}

$hasBlock = $false
foreach ($l in $lines) { if ($l -match "^\s*Match\s+User\s+$User\s*$") { $hasBlock = $true } }
if (-not $hasBlock) {
    $lines.Add("")
    $lines.Add("# marking program: sftp only, chroot C:\sync (deploy\SFTP_1C.md)")
    $lines.Add("Match User $User")
    $lines.Add("    AuthorizedKeysFile __PROGRAMDATA__/ssh/${User}_keys")
    $lines.Add("    ChrootDirectory $SyncRoot")
    $lines.Add("    ForceCommand internal-sftp")
    $lines.Add("    AllowTcpForwarding no")
    $lines.Add("    PermitTunnel no")
    $lines.Add("    PermitTTY no")
    $lines.Add("    X11Forwarding no")
    $lines.Add("    AllowAgentForwarding no")
}
# Без BOM: sshd читает байты, и BOM испортил бы первую строку конфига.
[System.IO.File]::WriteAllText($cfg, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding $false))
Ok "sshd_config обновлён (прежний: $backup)"

# --- 5. Открытый ключ ---
$keys = Join-Path $cfgDir "${User}_keys"
[System.IO.File]::WriteAllText($keys, $PublicKey + "`n", (New-Object System.Text.UTF8Encoding $false))
& icacls $keys /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "icacls $keys не прошёл" }
Ok "Открытый ключ положен: $keys"

# --- 6. Проверка, брандмауэр, служба ---
$test = & $sshd -t 2>&1
if ($LASTEXITCODE -ne 0) {
    Copy-Item $backup $cfg -Force
    Fail "sshd -t: $test — прежний sshd_config возвращён, служба не тронута."
}
Ok "sshd -t: конфиг корректен"
$rule = "SFTP marking $Port"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue) -and
    -not (Get-NetFirewallRule -DisplayName "SSH tunnel marking $Port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    Ok "Брандмауэр: входящие на $Port разрешены"
} else {
    Ok "Брандмауэр: правило на $Port уже есть"
}
if ($AllowFrom) {
    Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue |
        Set-NetFirewallAddressFilter -RemoteAddress $AllowFrom
    Ok "Брандмауэр: порт $Port — только с $AllowFrom"
    # Ограничение одного правила ничего не стоит, если тот же порт открывает
    # другое: брандмауэр пропускает, когда разрешает ХОТЯ БЫ ОДНО правило. На
    # боевом сервере так и было — 443 держало для всех правило IIS «Службы
    # Интернета (входящий трафик HTTPS)», оставшееся от роли веб-сервера.
    $others = Get-NetFirewallPortFilter | ? { $_.LocalPort -eq "$Port" } | Get-NetFirewallRule |
        ? { $_.DisplayName -ne $rule -and $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' }
    foreach ($o in $others) {
        Warn "Порт $Port открывает и правило «$($o.DisplayName)» — ограничение по адресу не сработает, пока оно включено. Выключить: Get-NetFirewallRule -DisplayName '$($o.DisplayName)' | Disable-NetFirewallRule"
    }
} else {
    Warn "Порт $Port открыт для всего интернета (вход только по ключу). Сузить: запустить снова с -AllowFrom <внешний адрес рабочего компьютера>."
}
Warn "Если на сервере включён брандмауэр Hetzner (robot.hetzner.com -> сервер -> Firewall), порт $Port нужно разрешить и там (deploy\SFTP_1C.md)."
Set-Service sshd -StartupType Automatic
Restart-Service sshd
Start-Sleep -Seconds 2
$up = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $up) { Fail "sshd не слушает $Port после перезапуска — журнал: Просмотр событий -> OpenSSH." }
Ok "sshd слушает порт $Port"

# --- 7. Отпечаток для сверки ---
Write-Host ""
Write-Host "Отпечаток ключа сервера — сверить посимвольно с тем, что напечатал рабочий компьютер:" -ForegroundColor Yellow
& (Join-Path $sshDir "ssh-keygen.exe") -l -f (Join-Path $cfgDir "ssh_host_ed25519_key.pub")
Write-Host ""
Ok "Сервер готов. Дальше — на рабочем компьютере: проверка sftp (SFTP_1C.md, шаг 6)."
