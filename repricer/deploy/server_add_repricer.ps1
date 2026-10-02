<#
.SYNOPSIS
    Сервер: доступ «Репрайсера» к обмену с 1С — две папки и второй ключ SFTP.

.DESCRIPTION
    Репрайсер ходит на сервер той же учётной записью, что «Маркировка»
    (marking_sftp, заперта в C:\sync, умеет только SFTP), своим ключом. Скрипт:
      1. проверяет, что SFTP «Маркировки» уже настроен (учётная запись и файл
         ключей есть) — иначе сначала marking\deploy\server_sftp_setup.ps1;
      2. создаёт C:\sync\results\pricing и C:\sync\archive\pricing — туда
         обработка 1С (mark-3) кладёт ответы репрайсеру, оттуда он их забирает
         и архивирует;
      3. даёт marking_sftp право менять ТОЛЬКО эти две папки (tasks у неё уже есть);
      4. ДОПИСЫВАЕТ открытый ключ репрайсера второй строкой в файл ключей —
         ключ «Маркировки» остаётся, права на файл не меняются.

    sshd_config, порт, брандмауэр, sync_admin и 1С не трогает. sshd
    перезапускать не нужно: ключи он читает при каждом входе. Повторный запуск
    безопасен — ключ второй раз не допишется.

.NOTES
    PowerShell ОТ АДМИНИСТРАТОРА. Ключ — строка ssh-ed25519 ... из вывода
    install_workstation.ps1 репрайсера на офисном компьютере:
        powershell -ExecutionPolicy Bypass -File C:\repricer_setup\server_add_repricer.ps1 -PublicKey "ssh-ed25519 AAAA... repricer@PC"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicKey,
    [string]$SyncRoot = "C:\sync",
    [string]$User = "marking_sftp"
)
$ErrorActionPreference = "Stop"
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Запустите PowerShell от администратора." }

$PublicKey = $PublicKey.Trim()
if ($PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$') {
    Fail "Это не открытый ключ: нужна одна строка «ssh-ed25519 AAAA... repricer@...» из вывода установщика."
}

# --- 1. SFTP «Маркировки» уже настроен? ---
if (-not (Get-LocalUser -Name $User -ErrorAction SilentlyContinue)) {
    Fail "Нет учётной записи ${User}: SFTP ещё не настроен. Сначала marking\deploy\server_sftp_setup.ps1 -PublicKey `"$PublicKey`" (ключ репрайсера подойдёт), затем этот скрипт."
}
$keys = Join-Path $env:ProgramData "ssh\${User}_keys"
if (-not (Test-Path $keys)) {
    Fail "Нет ${keys}: SFTP настроен не скриптом «Маркировки». Сначала marking\deploy\server_sftp_setup.ps1."
}
if (-not (Test-Path (Join-Path $SyncRoot "tasks"))) { Fail "Нет $SyncRoot\tasks — это не тот сервер или не тот каталог обмена." }
Ok "SFTP «Маркировки» на месте: $User, $keys"

# --- 2-3. Папки и права ---
foreach ($d in @("$SyncRoot\results\pricing", "$SyncRoot\archive\pricing")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    & icacls $d /grant "${User}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "icacls $d не прошёл" }
}
Ok "Папки results\pricing и archive\pricing: $User может их менять"

# --- 4. Второй ключ ---
$body = ($PublicKey -split ' ')[1]
$text = [System.IO.File]::ReadAllText($keys)
if ($text -match [regex]::Escape($body)) {
    Ok "Ключ репрайсера уже есть в $keys — не дописываю"
} else {
    $prefix = if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { "`n" } else { "" }
    # Без BOM и с LF: sshd читает байты, BOM испортил бы строку ключа.
    [System.IO.File]::AppendAllText($keys, $prefix + $PublicKey + "`n", (New-Object System.Text.UTF8Encoding $false))
    Ok "Ключ репрайсера дописан в $keys (ключ «Маркировки» на месте)"
}
$lines = @(Get-Content $keys | Where-Object { $_.Trim() }).Count
Ok "Ключей в файле: $lines. Готово — на офисном компьютере проверьте вход sftp (инструкция, шаг 3)."
