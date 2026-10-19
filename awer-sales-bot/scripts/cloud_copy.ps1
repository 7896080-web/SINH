# awer-sales-bot: daily DB snapshot -> backups\ (30 days) -> Selectel S3 (30 days). See BACKUPS.md.
# Run by the scheduled task "AwerSalesBot cloud backup" (SYSTEM, 05:00), created by scripts\install.ps1.
# ASCII on purpose: Windows PowerShell on the server breaks Cyrillic in task scripts without BOM.
# Only COPY to the cloud and delete there files of our own mask older than 30 days. Never "rclone sync".
param(
    [string]$App = "C:\awer-sales-bot",
    [string]$Rclone = "C:\sync_admin\tools\rclone.exe",
    [string]$Remote = "selectel:awer-backups/awer_sales_bot",
    [int]$KeepDays = 30
)
$ErrorActionPreference = "Continue"
$Log = Join-Path $App "logs\cloud_copy.log"
$Py = Join-Path $App ".venv\Scripts\python.exe"
$Conf = Join-Path $App "rclone.conf"
$Backups = Join-Path $App "backups"
New-Item -ItemType Directory -Force -Path (Join-Path $App "logs") | Out-Null

function L($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $Log -Append -Encoding ascii }

L "start"
Set-Location $App   # config.yaml and data\awer.db are relative to the program folder
# snapshot: Connection.backup() + PRAGMA integrity_check; result line (UTF-8) goes to logs\backup.log
& $Py -m awer_sales_bot.cli backup --dest $Backups --keep-days $KeepDays --log (Join-Path $App "logs\backup.log") | Out-Null
if ($LASTEXITCODE -ne 0) { L "snapshot FAILED (exit $LASTEXITCODE), see logs\backup.log"; exit 1 }
$last = Get-ChildItem $Backups -Filter "awer-*.db" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
L "snapshot ok: $($last.Name) $([math]::Round($last.Length / 1MB, 1)) MB"

# exit 2 = snapshot ok, but NO cloud copy: the task result is not 0, status.ps1 shows it (audit, section 4)
if (-not (Test-Path $Conf)) { L "WARNING no $Conf - cloud copy skipped (local copies only)"; exit 2 }
if (-not (Test-Path $Rclone)) { L "WARNING no $Rclone - cloud copy skipped (local copies only)"; exit 2 }
# rclone.conf copied by hand inherits open folder rights: only SYSTEM and Administrators (audit U9)
& icacls $Conf /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" /C /Q 2>&1 | Out-Null

# .env, config.yaml and rclone.conf never leave the server: only awer-*.db snapshots are copied
& $Rclone copy $Backups $Remote --config $Conf --include "awer-*.db" --log-file $Log --log-level NOTICE
if ($LASTEXITCODE -ne 0) { L "rclone copy FAILED (exit $LASTEXITCODE)"; exit 1 }
& $Rclone delete $Remote --config $Conf --include "awer-*.db" --min-age "$($KeepDays)d" --log-file $Log --log-level NOTICE
if ($LASTEXITCODE -ne 0) { L "rclone delete of old copies FAILED (exit $LASTEXITCODE)"; exit 1 }
L "cloud ok: $Remote"
