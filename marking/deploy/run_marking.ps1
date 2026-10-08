<#
.SYNOPSIS
    Запуск программы «Маркировка и поставки» на этом компьютере (ярлык «Маркировка»).

.DESCRIPTION
    Уже запущена — просто открывает браузер. Иначе поднимает программу в фоне
    (окна нет, журнал — logs\marking.log), ждёт, пока она ответит, и открывает
    http://127.0.0.1:<порт>. Слушает только 127.0.0.1: с других компьютеров
    программы не видно.
#>
param([int]$Port = 8001)
$Root = Split-Path $PSScriptRoot -Parent
$url = "http://127.0.0.1:$Port"

# Опознание, а не «кто-то ответил»: рядом «Репрайсер» (8002) и «Ввод в оборот»
# (8003) со своими /login. Чужая программа на 8001 открылась бы в браузере как
# «Маркировка» (08.10.2026 так столкнулись «Ввод в оборот» и «Репрайсер» на 8002).
function Test-Up {
    try {
        $r = Invoke-WebRequest "$url/login" -UseBasicParsing -TimeoutSec 2
        return ($r.Content -match "Маркировка и поставки")
    } catch { return $false }
}

if (-not (Test-Up)) {
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    $logs = Join-Path $Root "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Start-Process -FilePath $py -WorkingDirectory $Root -WindowStyle Hidden `
        -ArgumentList "-m", "uvicorn", "markapp.main:app", "--host", "127.0.0.1", "--port", "$Port" `
        -RedirectStandardOutput (Join-Path $logs "marking.log") `
        -RedirectStandardError (Join-Path $logs "marking.err.log")
    for ($i = 0; $i -lt 60 -and -not (Test-Up); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Up)) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("Программа не запустилась. Журнал: $logs\marking.err.log", "Маркировка") | Out-Null
        exit 1
    }
}
Start-Process $url
