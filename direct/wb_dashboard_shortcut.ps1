# Ярлык «Реклама WB» на рабочем столе: двойной щелчок пересобирает дашборд и открывает его в браузере
# (python wb_dashboard.py — без окна консоли). Иконка — C:\direct\wb_dashboard.ico.
# Установить:  powershell -ExecutionPolicy Bypass -File C:\direct\wb_dashboard_shortcut.ps1
# Убрать:      удалите ярлык «Реклама WB» с рабочего стола.

$dir = "C:\direct"
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host "Не найден python. Проверьте, что команда python работает."; exit 1 }
# pythonw.exe — тот же Python без чёрного окна консоли; если его нет — обычный python.exe
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
if (-not (Test-Path "$dir\wb_dashboard.py")) { Write-Host "Нет $dir\wb_dashboard.py — сначала скопируйте скрипты."; exit 1 }

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Реклама WB.lnk"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = "`"$dir\wb_dashboard.py`""
$s.WorkingDirectory = $dir
$s.Description = "Дашборд рекламы Wildberries: пересобрать и открыть"
if (Test-Path "$dir\wb_dashboard.ico") { $s.IconLocation = "$dir\wb_dashboard.ico,0" }
$s.Save()
Write-Host "Готово: ярлык «Реклама WB» на рабочем столе ($lnk)."
