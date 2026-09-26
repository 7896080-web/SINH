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
    param([string]$Folder, [string]$Title, [string]$Target, [string]$Browser)

    $shell = New-Object -ComObject WScript.Shell

    if ($Browser) {
        $linkPath = Join-Path $Folder ($Title + ".lnk")
        $link = $shell.CreateShortcut($linkPath)
        $link.TargetPath = $Browser
        # Адрес в кавычках: без них браузер получит два аргумента, если в
        # адресе однажды появится параметр с пробелом.
        $link.Arguments = '"' + $Target + '"'
        $link.IconLocation = "$Browser,0"
        $link.Description = "Админка синхронизации остатков — $Target"
        $link.WorkingDirectory = Split-Path $Browser
        $link.Save()
        return $linkPath
    }

    # Браузер не нашёлся — отдаём системному. Это ХУЖЕ (см. шапку), поэтому
    # скрипт об этом говорит вслух, а не делает вид, что всё в порядке.
    $urlPath = Join-Path $Folder ($Title + ".url")
    Set-Content -Path $urlPath -Encoding ASCII -Value @(
        "[InternetShortcut]",
        "URL=$Target"
    )
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
$created += New-AppShortcut -Folder $desktop.Path -Title $Name -Target $Url -Browser $browser

if ($Returns) {
    $returnsUrl = $Url.TrimEnd("/") + "/returns"
    $created += New-AppShortcut -Folder $desktop.Path -Title "Возвраты" -Target $returnsUrl -Browser $browser
}

Write-Host ""
foreach ($path in $created) {
    Write-Host "Создан ярлык: $path" -ForegroundColor Green
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
