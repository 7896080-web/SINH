<#
.SYNOPSIS
    Установка «Ввода в оборот» (коды вне Lamoda) на рабочий компьютер с КриптоПро.

.DESCRIPTION
    Отдельная программа: своя папка (C:\kiz_intro), своя база, свой ключ
    шифрования, порт 8002, свой ярлык. «Маркировку» не трогает.
      1. Находит Python 3.11+.
      2. Создаёт .venv и ставит зависимости.
      3. Создаёт .env со своим ключом шифрования (если .env нет).
      4. Гоняет тесты.
      5. Кладёт ярлык «Ввод в оборот» на рабочий стол.
    Идемпотентен: существующие .env и база не трогаются.

    powershell -ExecutionPolicy Bypass -File C:\kiz_intro\deploy\install.ps1
#>
[CmdletBinding()]
param([int]$Port = 8002)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; exit 1 }

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
if (-not (Test-Path (Join-Path $Root "kizapp\web.py"))) { Fail "В $Root нет kizapp\web.py" }

$py = $null
foreach ($c in @("python", "py")) {
    try { $v = & $c --version 2>&1; if ($v -match "Python 3\.(1[1-9]|[2-9]\d)") { $py = (Get-Command $c).Source; break } } catch { }
}
if (-not $py) { Fail "Не найден Python 3.11+ (python.org, галочка «Add python.exe to PATH»)" }
Ok "Python: $py"

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Info "Создаю venv"
    & $py -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "venv не создан" }
}
Info "Ставлю зависимости"
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой" }
Ok "Зависимости установлены"

$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    $key = & $venvPy -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    Set-Content -Path $envFile -Value @("KIZ_SECRETS_KEY=$key", "KIZ_PORT=$Port") -Encoding UTF8
    Ok ".env создан"
    Warn "СОХРАНИТЕ KIZ_SECRETS_KEY из $envFile туда, где храните пароли: без него коды в базе не прочитать."
} else {
    Ok ".env уже есть — не трогаю"
}

Info "Тесты"
& $venvPy -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { Fail "Тесты красные — установку не заканчиваю" }
Ok "Тесты зелёные"

$run = Join-Path $Root "deploy\run.ps1"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Ввод в оборот.lnk"))
$s.TargetPath = "powershell.exe"
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`" -Port $Port"
$s.WorkingDirectory = $Root
$s.Description = "Ввод в оборот кодов вне Lamoda"
$s.IconLocation = (Join-Path $Root "kizapp\static\favicon.ico")
$s.Save()
Ok "Ярлык «Ввод в оборот» на рабочем столе. Адрес: http://127.0.0.1:$Port"
