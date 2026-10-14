<#
.SYNOPSIS
    Офисный компьютер: собрать «Репрайсер» для переноса на сервер и ВЫКЛЮЧИТЬ его здесь.

.DESCRIPTION
    Порядок важен: сначала программа здесь останавливается насовсем, и только потом
    снимается копия базы. Два работающих экземпляра (здесь и на сервере) слали бы на
    площадки одни и те же цены и задания в 1С параллельно, а копия, снятая с живой
    программы, потеряла бы то, что она запишет после копии.

    1. Останавливает программу и убирает её из автозапуска.
    2. Кладёт MOVED_TO_SERVER.txt: ярлык и update_workstation.ps1 здесь больше не
       запускают программу (говорят, что она на сервере).
    3. Снимает копию базы штатным механизмом SQLite и проверяет её.
    4. Собирает архив: код, копия базы, .env, история эталона. Без .venv, логов,
       ключа SFTP (на сервере 1С рядом, SFTP не нужен) и старых копий базы.

    В архиве .env — ключ шифрования и ключи площадок. Переносить только по RDP
    (копировать-вставить файл в окно сервера), никуда не выкладывать, после
    установки на сервере удалить.

    powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\export_for_server.ps1
#>
[CmdletBinding()]
param(
    [int]$WebPort = 8002,
    [string]$Out = "C:\temp\repricer_move.zip"
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "Нет $venvPy — это не установленный репрайсер" }
if (-not (Test-Path (Join-Path $Root ".env"))) { Fail "Нет $Root\.env — без него на сервере не прочитать ключи площадок" }

# --- 1. Остановить и убрать из автозапуска ---
Info "Останавливаю программу"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*priceapp.main:app*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2
$still = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*priceapp.main:app*" }
if ($still) { Fail "Программа не остановилась — закройте её и запустите скрипт снова" }
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "Репрайсер.lnk"
if (Test-Path $startup) { Remove-Item $startup -Force; Ok "Убрана из автозапуска" }

# --- 2. Метка «переехала» ---
$stamp = Get-Date -Format "dd.MM.yyyy HH:mm"
Set-Content -Path (Join-Path $Root "MOVED_TO_SERVER.txt") -Encoding UTF8 -Value @(
    "Перенесена на сервер $stamp (deploy\export_for_server.ps1).",
    "Работать с программой — на сервере, ярлык «Репрайсер» на его рабочем столе.",
    "Этот экземпляр не запускать: он слал бы цены параллельно с серверным.")
Ok "Здесь программа больше не запустится (MOVED_TO_SERVER.txt)"

# --- 3. Копия базы ---
Info "Копия базы"
$stage = Join-Path $env:TEMP ("repricer_move_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$dbCopy = Join-Path $stage "repricer.db"
& $venvPy -c @"
import sqlite3, sys
src = sqlite3.connect(r'$Root\repricer.db'); dst = sqlite3.connect(r'$dbCopy')
src.backup(dst); dst.execute('PRAGMA journal_mode=DELETE')
ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
n = dst.execute('SELECT count(*) FROM accounts').fetchone()[0]
dst.close(); src.close()
print(f'integrity {ok}, кабинетов {n}')
sys.exit(0 if ok == 'ok' else 1)
"@
if ($LASTEXITCODE -ne 0) { Fail "Копия базы не прошла проверку — архив не собран" }
Ok "Копия базы снята и проверена"

# --- 4. Архив ---
Info "Собираю архив"
$skipDirs = @(".venv", "logs", "ssh", "backups", "onec_archive", ".pytest_cache", "__pycache__")
$skipFiles = @("repricer.db", "repricer.db-wal", "repricer.db-shm", "MOVED_TO_SERVER.txt")
$code = Join-Path $stage "repricer"
New-Item -ItemType Directory -Force -Path $code | Out-Null
Get-ChildItem $Root -Recurse -File -Force | Where-Object {
    $rel = $_.FullName.Substring($Root.Length + 1)
    $parts = $rel -split '\\'
    -not ($parts | Where-Object { $skipDirs -contains $_ }) -and -not ($skipFiles -contains $rel) -and
        $_.Name -notlike "*.bak_*"
} | ForEach-Object {
    $rel = $_.FullName.Substring($Root.Length + 1)
    $dst = Join-Path $code $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst
}
Move-Item $dbCopy (Join-Path $code "repricer.db")
New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
if (Test-Path $Out) { Remove-Item $Out -Force }
# ZipFile, а не Compress-Archive: тот в PowerShell 5.1 пропускает скрытые файлы и
# портит русские имена файлов.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($code, $Out)
Remove-Item $stage -Recurse -Force
$mb = [math]::Round((Get-Item $Out).Length / 1MB, 1)
Ok "Архив: $Out ($mb МБ)"
Warn "В архиве .env с ключами площадок: переносите только по RDP, после установки на сервере удалите архив и здесь, и там."
Write-Host ""
Write-Host "Дальше на сервере (RDP, PowerShell от администратора):" -ForegroundColor Cyan
Write-Host "  1. Скопировать $Out на сервер в C:\temp\"
Write-Host "  2. Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::ExtractToDirectory('C:\temp\repricer_move.zip', 'C:\repricer')"
Write-Host "  3. powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\install_server.ps1"
