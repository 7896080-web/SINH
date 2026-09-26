# Ярлык «Синхронизация остатков» на рабочем столе.
#
# Зачем скриптом, а не «перетащите ссылку из браузера».
#
# Обычный интернет-ярлык открывается браузером ПО УМОЛЧАНИЮ, а на Windows
# Server 2016 это Internet Explorer с усиленной конфигурацией безопасности:
# страницы админки на нём либо не откроются вовсе, либо откроются без htmx —
# то есть фильтры, автообновление и массовые правки просто не сработают, и
# выглядеть это будет как поломка приложения. Поэтому браузер выбирается ЯВНО и
# записывается в ярлык: найденный современный, а не тот, что назначен в системе.
#
# Значок — СВОЙ, из `deploy\*.ico`, а не иконка браузера. Ярлыков на столе два
# («Синхронизация остатков» и «Возвраты»), и с иконкой браузера они выглядят как
# два одинаковых Chrome: какой куда ведёт, видно только по подписи, а подпись в
# панели задач не показывают вовсе. Значки различаются ФОРМОЙ, а не только
# цветом (стрелки обмена против коробки): по RDP цвет сжимается сильнее всего.
# Рисует их `scripts/gen_icons.py` — картинка описана арифметикой и
# пересобирается из репозитория, поэтому её можно перекрасить и объяснить.
#
# Путь к значку запоминается в ярлыке АБСОЛЮТНЫЙ, то есть файл обязан остаться
# на месте: удалят `C:\sync_admin\deploy\sync_admin.ico` — значок пропадёт у
# уже созданного ярлыка. Ближайший накат положит файл обратно.
#
# Адрес — КОРЕНЬ, а не конкретная страница: он сам разводит по ролям
# (администратор → «Мэппинг», складская учётная запись → «Возвраты»). Поставь мы
# в ярлык `/returns`, администратор каждый раз попадал бы не туда, а сделай
# наоборот — кладовщик получал бы отказ сразу после входа и решил, что учётная
# запись не работает.
#
# Кладём на ОБЩИЙ рабочий стол, если он доступен: склад работает под общей
# учётной записью, но заходить на сервер могут и под другой, а ярлык, который
# видно не всем, приходится заводить заново каждому.
#
# Запускать от администратора (иначе общий рабочий стол недоступен — скрипт
# молча положит ярлык на текущий и скажет об этом).
#
#   powershell -ExecutionPolicy Bypass -File C:\sync_admin\deploy\create_desktop_shortcut.ps1
#
# Параметры:
#   -Url        адрес, по умолчанию http://127.0.0.1:8000
#   -Name       имя ярлыка, по умолчанию «Синхронизация остатков»
#   -Returns    добавить ВТОРОЙ ярлык сразу на «Возвраты» (для складской машины)

param(
    [string]$Url = "http://127.0.0.1:8000",
    [string]$Name = "Синхронизация остатков",
    [switch]$Returns
)

$ErrorActionPreference = "Stop"

# Запуск ФАЙЛОМ даёт $PSScriptRoot, вставка блоков в консоль — нет. Во втором
# случае брать неоткуда, кроме штатного пути установки.
$here = $PSScriptRoot
if (-not $here) { $here = "C:\sync_admin\deploy" }

# --------------------------------------------------------------- браузер

# Порядок намеренный: сначала то, что почти наверняка стоит на сервере и умеет
# современную разметку. Internet Explorer в списке НЕТ и быть не должно —
# лучше отдать ярлык системному браузеру (вдруг там настроено осмысленно), чем
# записать в него IE своими руками.
function Find-Browser {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:LOCALAPPDATA\Yandex\YandexBrowser\Application\browser.exe",
        "$env:ProgramFiles\Yandex\YandexBrowser\Application\browser.exe",
        "${env:ProgramFiles(x86)}\Yandex\YandexBrowser\Application\browser.exe",
        "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
        "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe"
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) { return $path }
    }
    return $null
}

# --------------------------------------------------------------- значок

# Отсутствие значка НЕ отменяет ярлык: без него ярлык рабочий, просто с иконкой
# браузера. Но сказать об этом надо вслух — иначе человек решит, что скрипт
# сломан, тогда как сломан один файл, который вернёт ближайший накат.
function Find-Icon {
    param([string]$FileName)
    $path = Join-Path $here $FileName
    if (Test-Path $path) { return $path }
    Write-Host "Значок не найден: $path — ярлык получит иконку браузера." -ForegroundColor Yellow
    Write-Host "Файл кладёт накат; проверьте, что патч применялся целиком." -ForegroundColor Yellow
    return $null
}

# --------------------------------------------------------------- рабочий стол

function Get-DesktopFolder {
    $public = Join-Path $env:PUBLIC "Desktop"
    if (Test-Path $public) {
        # Проверяем не наличие папки, а ПРАВО ПИСАТЬ в неё: без прав
        # администратора она существует, но запись в неё падает — и ярлык
        # «создался» бы только в выводе скрипта.
        $probe = Join-Path $public (".sync_admin_probe_" + [guid]::NewGuid().ToString("N"))
        try {
            New-Item -Path $probe -ItemType File -Force | Out-Null
            Remove-Item $probe -Force
            return @{ Path = $public; Shared = $true }
        } catch {
            # прав нет — молча уходим на личный рабочий стол
        }
    }
    return @{ Path = [Environment]::GetFolderPath("Desktop"); Shared = $false }
}

# --------------------------------------------------------------- создание

function New-AppShortcut {
    param([string]$Folder, [string]$Title, [string]$Target, [string]$Browser, [string]$Icon)

    $shell = New-Object -ComObject WScript.Shell

    if ($Browser) {
        $linkPath = Join-Path $Folder ($Title + ".lnk")
        # Старый ярлык сносим, а не переписываем поверх: Windows помнит значок
        # по пути ярлыка, и перезапись того же файла оставляла бы на столе
        # прежнюю картинку — выглядит это ровно как «скрипт ничего не сделал».
        if (Test-Path $linkPath) { Remove-Item $linkPath -Force }
        $link = $shell.CreateShortcut($linkPath)
        $link.TargetPath = $Browser
        # Адрес в кавычках: без них браузер получит два аргумента, если в
        # адресе однажды появится параметр с пробелом.
        $link.Arguments = '"' + $Target + '"'
        if ($Icon) { $link.IconLocation = "$Icon,0" } else { $link.IconLocation = "$Browser,0" }
        $link.Description = "Админка синхронизации остатков — $Target"
        $link.WorkingDirectory = Split-Path $Browser
        $link.Save()
        return $linkPath
    }

    # Браузер не нашёлся — отдаём системному. Это ХУЖЕ (см. шапку), поэтому
    # скрипт об этом говорит вслух, а не делает вид, что всё в порядке.
    $urlPath = Join-Path $Folder ($Title + ".url")
    $lines = @("[InternetShortcut]", "URL=$Target")
    if ($Icon) {
        # У .url значок задаётся своими полями, и IconIndex обязателен: без
        # него часть оболочек берёт нулевой кадр не из того файла.
        $lines += "IconFile=$Icon"
        $lines += "IconIndex=0"
    }
    # ASCII намеренно: в файле только латиница и цифры, а .url в UTF-8 с BOM
    # часть оболочек читать отказывается.
    Set-Content -Path $urlPath -Encoding ASCII -Value $lines
    return $urlPath
}

# --------------------------------------------------------------- выполнение

$browser = Find-Browser
$desktop = Get-DesktopFolder

if ($browser) {
    Write-Host "Браузер: $browser" -ForegroundColor Green
} else {
    Write-Host "Современный браузер не найден — ярлык откроется браузером по умолчанию." -ForegroundColor Yellow
    Write-Host "На Windows Server это обычно Internet Explorer, и админка на нём работать не будет." -ForegroundColor Yellow
    Write-Host "Поставьте Chrome, Edge или Яндекс.Браузер и запустите скрипт ещё раз." -ForegroundColor Yellow
}

if ($desktop.Shared) {
    Write-Host "Рабочий стол: общий ($($desktop.Path)) — ярлык увидят все пользователи." -ForegroundColor Green
} else {
    Write-Host "Рабочий стол: личный ($($desktop.Path))." -ForegroundColor Yellow
    Write-Host "Чтобы ярлык был у всех, запустите скрипт от администратора." -ForegroundColor Yellow
}

$created = @()
$created += New-AppShortcut -Folder $desktop.Path -Title $Name -Target $Url `
    -Browser $browser -Icon (Find-Icon "sync_admin.ico")

if ($Returns) {
    $returnsUrl = $Url.TrimEnd("/") + "/returns"
    $created += New-AppShortcut -Folder $desktop.Path -Title "Возвраты" -Target $returnsUrl `
        -Browser $browser -Icon (Find-Icon "returns.ico")
}

Write-Host ""
foreach ($path in $created) {
    Write-Host "Создан ярлык: $path" -ForegroundColor Green
}

# Кэш значков Windows помнит картинку по ПУТИ файла, а путь у нас постоянный:
# перерисованный накатом значок остался бы старым на экране, и выглядело бы
# это как невыполненный накат. Сбой тут ничего не значит — самое позднее
# картинка обновится после входа в систему, — поэтому он и проглатывается.
try {
    Start-Process -FilePath "ie4uinit.exe" -ArgumentList "-show" -NoNewWindow -Wait -ErrorAction Stop
} catch {
    Write-Host "Кэш значков обновить не удалось — если картинка старая, перезайдите в систему." -ForegroundColor Yellow
}

# Проверка ЖИВОСТИ, а не только файла: ярлык на неотвечающий адрес выглядит
# точно так же, как исправный, и человек узнает об этом, нажав на него.
Write-Host ""
try {
    $response = Invoke-WebRequest -Uri ($Url.TrimEnd("/") + "/health") -UseBasicParsing -TimeoutSec 10
    Write-Host "Приложение отвечает ($($response.StatusCode)) — ярлык откроет рабочую страницу." -ForegroundColor Green
} catch {
    Write-Host "ВНИМАНИЕ: приложение по адресу $Url сейчас НЕ отвечает." -ForegroundColor Red
    Write-Host "Ярлык создан и будет работать, когда поднимется служба sync_admin_web." -ForegroundColor Red
    Write-Host "Проверьте: nssm status sync_admin_web" -ForegroundColor Red
}
