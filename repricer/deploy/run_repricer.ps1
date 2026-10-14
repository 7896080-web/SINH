<#
.SYNOPSIS
    Запуск программы «Репрайсер» на этом компьютере (ярлык «Репрайсер»).

.DESCRIPTION
    Уже запущена — просто открывает браузер. Иначе поднимает программу в фоне
    (окна нет, журнал — logs\repricer.log), ждёт, пока она ответит, и открывает
    http://127.0.0.1:<порт>. Слушает только 127.0.0.1: с других компьютеров
    программы не видно.
#>
param([int]$Port = 8002)
$Root = Split-Path $PSScriptRoot -Parent
$url = "http://127.0.0.1:$Port"

# Программа переехала на сервер (deploy\export_for_server.ps1): второй экземпляр здесь
# слал бы на площадки те же цены и задания в 1С параллельно с серверным.
$moved = Join-Path $Root "MOVED_TO_SERVER.txt"
if (Test-Path $moved) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Репрайсер перенесён на сервер и здесь не запускается.`n`n$((Get-Content $moved -Encoding UTF8) -join "`n")", "Репрайсер") | Out-Null
    exit 1
}

function Test-Up {
    try { Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if (-not (Test-Up)) {
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    $logs = Join-Path $Root "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Start-Process -FilePath $py -WorkingDirectory $Root -WindowStyle Hidden `
        -ArgumentList "-m", "uvicorn", "priceapp.main:app", "--host", "127.0.0.1", "--port", "$Port" `
        -RedirectStandardOutput (Join-Path $logs "repricer.log") `
        -RedirectStandardError (Join-Path $logs "repricer.err.log")
    for ($i = 0; $i -lt 60 -and -not (Test-Up); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Up)) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("Программа не запустилась. Журнал: $logs\repricer.err.log", "Репрайсер") | Out-Null
        exit 1
    }
}
Start-Process $url
