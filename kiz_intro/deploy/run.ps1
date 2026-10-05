<#
.SYNOPSIS
    Запуск «Ввода в оборот» (ярлык). Уже запущена — просто открывает браузер.
    Слушает только 127.0.0.1: с других компьютеров программы не видно.
#>
param([int]$Port = 8002)
$Root = Split-Path $PSScriptRoot -Parent
$url = "http://127.0.0.1:$Port"

function Test-Up {
    try { Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if (-not (Test-Up)) {
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    $logs = Join-Path $Root "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Start-Process -FilePath $py -WorkingDirectory $Root -WindowStyle Hidden `
        -ArgumentList "-m", "uvicorn", "kizapp.web:app", "--host", "127.0.0.1", "--port", "$Port" `
        -RedirectStandardOutput (Join-Path $logs "kiz.log") -RedirectStandardError (Join-Path $logs "kiz.err.log")
    for ($i = 0; $i -lt 60 -and -not (Test-Up); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Up)) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("Программа не запустилась. Журнал: $logs\kiz.err.log", "Ввод в оборот") | Out-Null
        exit 1
    }
}
Start-Process $url
