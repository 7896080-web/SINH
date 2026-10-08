<#
.SYNOPSIS
    Запуск «Ввода в оборот» (ярлык). Уже запущена — просто открывает браузер.
    Слушает только 127.0.0.1: с других компьютеров программы не видно.

.DESCRIPTION
    Порт проверяется ОПОЗНАНИЕМ, а не «кто-то ответил»: на этом компьютере
    рядом «Маркировка» (8001) и «Репрайсер» (8002). 08.10.2026 «Ввод в оборот»
    стоял на 8002, порт держал «Репрайсер», и ярлык полминуты ждал, а потом
    писал «не запустилась» без причины. Порт занят чужой программой — сразу
    говорим, какой.
#>
param([int]$Port = 8003)
$Root = Split-Path $PSScriptRoot -Parent
$url = "http://127.0.0.1:$Port"

function Test-Up {
    try {
        $r = Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 2
        return ($r.Content -match '"app"\s*:\s*"kiz_intro"')
    } catch { return $false }
}

function Show-Error($text) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($text, "Ввод в оборот") | Out-Null
    exit 1
}

if (-not (Test-Up)) {
    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($busy) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($busy.OwningProcess)"
        if ($p -and $p.CommandLine -notlike "*kizapp.web:app*") {
            Show-Error ("Порт $Port занят другой программой: $($p.Name) (процесс $($p.ProcessId))`n$($p.CommandLine)`n`n" +
                        "«Ввод в оборот» не запущен. Закройте ту программу или запустите этот с другим портом.")
        }
    }
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    $logs = Join-Path $Root "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Start-Process -FilePath $py -WorkingDirectory $Root -WindowStyle Hidden `
        -ArgumentList "-m", "uvicorn", "kizapp.web:app", "--host", "127.0.0.1", "--port", "$Port" `
        -RedirectStandardOutput (Join-Path $logs "kiz.log") -RedirectStandardError (Join-Path $logs "kiz.err.log")
    for ($i = 0; $i -lt 60 -and -not (Test-Up); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Up)) { Show-Error "Программа не запустилась. Журнал: $logs\kiz.err.log" }
}
Start-Process $url
