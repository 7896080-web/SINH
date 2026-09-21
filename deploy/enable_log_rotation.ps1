# Ротация логов для УЖЕ УСТАНОВЛЕННЫХ служб.
#
# Установщик (install_windows.ps1) настраивает её сам, но на боевом сервере он
# отработал раньше, чем эта настройка появилась. Скрипт доносит её до живых
# служб, ничего больше не трогая.
#
# Без ротации worker.err.log растёт, пока есть диск: однажды его нельзя будет
# ни открыть, ни найти в нём строку — то есть именно тогда, когда он нужен.
#
# Запускать от администратора. Перезапуск служб НЕ требуется: AppRotateOnline
# позволяет NSSM перекладывать файл на ходу.

$ErrorActionPreference = "Stop"
$limitBytes = 10485760      # 10 МБ на файл

foreach ($name in @("sync_admin_web", "sync_admin_worker")) {
    if (-not (Get-Service -Name $name -ErrorAction SilentlyContinue)) {
        Write-Host "служба $name не найдена — пропуск" -ForegroundColor Yellow
        continue
    }
    & nssm set $name AppRotateFiles 1 | Out-Null
    & nssm set $name AppRotateOnline 1 | Out-Null
    & nssm set $name AppRotateBytes $limitBytes | Out-Null

    $rotate = (& nssm get $name AppRotateFiles).Trim()
    $bytes  = (& nssm get $name AppRotateBytes).Trim()
    Write-Host "$name : AppRotateFiles=$rotate AppRotateBytes=$bytes" -ForegroundColor Green
}

Write-Host ""
Write-Host "Готово. Текущие логи будут переложены, когда дорастут до 10 МБ." -ForegroundColor Cyan
