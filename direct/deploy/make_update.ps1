<#
.SYNOPSIS
    Рабочий компьютер: собрать обновление скриптов рекламы для сервера (только код — без токенов,
    баз и ваших таблиц).

.DESCRIPTION
    После переезда скрипты работают на сервере, а свежий код приходит сюда, в C:\sinh (git pull).
    Этот скрипт берёт из C:\sinh\direct код (*.py, *.ps1, значок, requirements.txt, tools\, deploy\)
    и складывает в архив C:\temp\direct_update.zip с отметкой версии (VERSION.txt). Токенов и данных
    в архиве нет — его можно переносить как угодно; проще всего по RDP (копировать-вставить).

        powershell -ExecutionPolicy Bypass -File C:\sinh\direct\deploy\make_update.ps1

    Дальше на сервере: архив — в C:\temp\, затем (от администратора)
        powershell -ExecutionPolicy Bypass -File C:\direct\deploy\update_server.ps1
#>
[CmdletBinding()]
param(
    [string]$Repo = "C:\sinh",
    [string]$Out = "C:\temp\direct_update.zip",
    [switch]$NoPull
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$src = Join-Path $Repo "direct"
if (-not (Test-Path (Join-Path $src "wb_bids.py"))) { Fail "В $src нет wb_bids.py — это не копия репозитория (укажите -Repo)" }
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git -and -not $NoPull) {
    Info "git pull"
    & git -C $Repo pull --ff-only
    if ($LASTEXITCODE -ne 0) { Fail "git pull не прошёл — обновите $Repo вручную и запустите снова с -NoPull" }
}
$ver = "без git"
if ($git) { $ver = (& git -C $Repo log -1 --format="%h %cd" --date=format:"%d.%m.%Y %H:%M") -join "" }

$stage = Join-Path $env:TEMP ("direct_update_" + [guid]::NewGuid().ToString("N"))
$code = Join-Path $stage "direct"
New-Item -ItemType Directory -Force -Path $code | Out-Null
foreach ($f in Get-ChildItem -LiteralPath $src -File) {
    if ($f.Extension -in @(".py", ".ps1", ".ico") -or $f.Name -eq "requirements.txt") {
        Copy-Item -LiteralPath $f.FullName (Join-Path $code $f.Name)
    }
}
foreach ($d in @("deploy", "tools")) {
    $from = Join-Path $src $d
    if (-not (Test-Path $from)) { continue }
    foreach ($f in Get-ChildItem -LiteralPath $from -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' }) {
        $rel = $f.FullName.Substring($src.Length + 1)
        $dst = Join-Path $code $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Copy-Item -LiteralPath $f.FullName $dst
    }
}
foreach ($bad in @("wb_tokens.txt", "yandex_delivery_token.txt", "crm_config.json")) {
    if (Test-Path (Join-Path $code $bad)) { Fail "В обновление попал $bad — так быть не должно, архив не собран" }
}
$n = @(Get-ChildItem -LiteralPath $code -File -Recurse).Count
Set-Content -Path (Join-Path $code "VERSION.txt") -Encoding UTF8 -Value "$ver (собрано $(Get-Date -Format 'dd.MM.yyyy HH:mm'), файлов $n)"
New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null
if (Test-Path $Out) { Remove-Item $Out -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($code, $Out)
Remove-Item $stage -Recurse -Force
Ok "Обновление: $Out — версия $ver, файлов $n"
Write-Host ""
Write-Host "Дальше на сервере (RDP): скопировать $Out в C:\temp\, затем PowerShell от администратора:" -ForegroundColor Cyan
Write-Host "  powershell -ExecutionPolicy Bypass -File C:\direct\deploy\update_server.ps1"
