"""Патч обновления скриптов рекламы для сервера (C:\\direct) — один .ps1 вместо make_update.ps1 + архива.

Как у патчей sync_admin (scripts/gen_whole_file_patch.py): файлы целиком, сжатые (deflate) в base64, блоками с SHA256.
Патч везёт только файлы, изменённые с версии, что стоит на сервере (--base), а про все остальные скрипты знает их
SHA256 (без учёта CRLF/LF — копия репозитория на Windows хранит CRLF) и, если на сервере хоть один не такой или его
нет, ничего не трогает: новый код рядом со старым чужим — хуже, чем старый целиком.

На сервере (PowerShell от администратора) — файлом:
    powershell -ExecutionPolicy Bypass -File C:\\temp\\direct_patch_<tag>.ps1
или вставкой: БЛОК 1 … БЛОК N (сверяя SHA256), затем БЛОК APPLY. Порядок как у update_server.ps1: проверка без
изменений → выключить расписание AWER_WB_* и дождаться скриптов → прежний код в code_backups\\<время> (его вернёт
update_server.ps1 -Rollback) → новые файлы → скрипты загружаются? (нет — вернуть прежние) → wb_schedule.ps1 -Server,
ярлык, VERSION.txt.

    python deploy/make_patch.py --base abbd7db --tag dp01 --out /путь/direct_patch_dp01.ps1 [--also ФАЙЛ ...]
"""
import argparse
import base64
import hashlib
import os
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # …/direct
BLOCK = 20000          # символов base64 в блоке: столько PowerShell принимает вставкой без задержек
WIDTH = 80
CODE_EXT = (".py", ".ps1", ".ico")

APPLY = r'''# {tag}: запись файлов патча в C:\direct. check — только проверка; write — запись. Запускает БЛОК APPLY.
import base64, datetime, hashlib, io, os, py_compile, sys, tempfile
TAG = {tag!r}
VERSION = {version!r}
FILES = {files!r}
KEEP = {keep!r}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def norm(b):
    return sha(b.replace(b"\r\n", b"\n"))


def read(path):
    with io.open(path, "rb") as f:
        return f.read()


mode, root = sys.argv[1], sys.argv[2]
new = []
for rel, expect, kind, body in FILES:
    data = body.encode("utf-8") if kind == "t" else base64.b64decode(body)
    if sha(data) != expect:
        print("  ПАТЧ ПОВРЕЖДЁН:", rel)
        sys.exit(3)
    new.append((rel, data))

if mode == "check":
    bad = []
    for rel, expect in KEEP:
        path = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.exists(path):
            bad.append("нет файла  " + rel)
        elif norm(read(path)) != expect:
            bad.append("не та версия  " + rel)
    tmp = tempfile.mkdtemp(prefix="direct_" + TAG + "_")
    for rel, data in new:
        if rel.endswith(".py"):
            p = os.path.join(tmp, os.path.basename(rel))
            with io.open(p, "wb") as f:
                f.write(data)
            try:
                py_compile.compile(p, cfile=p + "c", doraise=True)
            except py_compile.PyCompileError as e:
                bad.append("не собирается  " + rel + ": " + str(e))
    todo = 0
    for rel, data in new:
        path = os.path.join(root, rel.replace("/", os.sep))
        same = os.path.exists(path) and norm(read(path)) == norm(data)
        todo += not same
        print("  " + ("уже стоит " if same else ("заменю    " if os.path.exists(path) else "добавлю   ")) + rel)
    print("  остальные скрипты (%d) — %s" % (len(KEEP), "той версии, под которую собран патч" if not bad else "НЕ ТЕ:"))
    for b in bad:
        print("    " + b)
    if bad:
        sys.exit(2)
    print("  менять файлов: %d" % todo)
    sys.exit(0)

if mode == "write":
    n = 0
    for rel, data in new:
        path = os.path.join(root, rel.replace("/", os.sep))
        if os.path.exists(path) and norm(read(path)) == norm(data):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "wb") as f:
            f.write(data)
        if sha(read(path)) != sha(data):
            print("  НЕ ЗАПИСАЛСЯ:", rel)
            sys.exit(4)
        print("  записан", rel)
        n += 1
    stamp = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    with io.open(os.path.join(root, "VERSION.txt"), "w", encoding="utf-8-sig") as f:
        f.write("%s (патч %s, поставлен %s)\n" % (VERSION, TAG, stamp))
    print("  записано файлов: %d" % n)
    sys.exit(0)
sys.exit(1)
'''

PS_HEAD = r'''# ===== {tag}: патч скриптов рекламы для C:\direct — версия {version} =====
# Сервер, PowerShell ОТ АДМИНИСТРАТОРА. Два способа (любой):
#   А) файлом: скопировать этот файл по RDP в C:\temp\ и запустить
#        powershell -ExecutionPolicy Bypass -File C:\temp\{name}
#   Б) вставкой: по очереди БЛОК 1 … БЛОК {n} (после каждого — сверить got/expect), затем БЛОК APPLY.
# Привозит: {files_list}.
# Что делает: проверяет патч и что остальные скрипты на сервере — той версии, под которую он собран (иначе ничего
# не трогает); выключает расписание AWER_WB_* и ждёт, пока доработают скрипты (до 30 мин; открытую страницу
# «Реклама WB» не ждёт); прежний код — в C:\direct\code_backups\<время>; кладёт новые файлы; не загружаются —
# сам возвращает прежние; ставит расписание (wb_schedule.ps1 -Server) и ярлык «Реклама WB».
# Данные (базы, состояние автопилота, журналы, токены) не трогает. Откат (сразу, без пробного прогона):
#   powershell -ExecutionPolicy Bypass -File C:\direct\deploy\update_server.ps1 -Rollback
function Hx($s) {{
    $h = [System.Security.Cryptography.SHA256]::Create()
    ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
}}
'''

PS_PART = r'''
# ---- БЛОК {i} из {n} ----
${tag}_{i} = @'
{body}
'@
${tag}_{i} = ${tag}_{i} -replace "\s",""
"{tag} блок {i} got    " + (Hx ${tag}_{i})
"{tag} блок {i} expect {sha}"
'''

PS_APPLY = r'''
# ---- БЛОК APPLY ----
function Invoke-{Tag} {{
    $ErrorActionPreference = "Stop"
    $Root = "C:\direct"
    $WaitMin = 30
    $work = "C:\temp\direct_patch_{tag}"
    function Info($m) {{ Write-Host "[*] $m" -ForegroundColor Cyan }}
    function Ok($m)   {{ Write-Host "[OK] $m" -ForegroundColor Green }}
    function Warn($m) {{ Write-Host "[!] $m" -ForegroundColor Yellow }}
    function Fail($m) {{ throw $m }}
    function Run($exe, [string[]]$a) {{
        # прямо в консоль (не через конвейер): кириллица не портится, а вывод не становится результатом функции.
        # Start-Process склеивает аргументы через пробел без кавычек — с пробелами берём в кавычки сами
        $a = @($a | ForEach-Object {{ if ($_ -match '\s') {{ '"' + $_ + '"' }} else {{ $_ }} }})
        $p = Start-Process -FilePath $exe -ArgumentList $a -WorkingDirectory $Root -NoNewWindow -PassThru   # import wb_* — из C:\direct
        $null = $p.Handle                    # иначе в PowerShell 5.1 ExitCode бывает пустым
        $p.WaitForExit()
        return $p.ExitCode
    }}
    function Code-Files($dir) {{
        # как в deploy\update_server.ps1: код, а не данные
        $base = (Get-Item -LiteralPath $dir).FullName.TrimEnd("\")
        $files = @(Get-ChildItem -LiteralPath $base -File | Where-Object {{ $_.Extension -in @(".py", ".ps1", ".ico") -or $_.Name -in @("requirements.txt", "VERSION.txt") }})
        foreach ($d in @("deploy", "tools")) {{
            $p = Join-Path $base $d
            if (Test-Path $p) {{ $files += @(Get-ChildItem -LiteralPath $p -File -Recurse | Where-Object {{ $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' }}) }}
        }}
        foreach ($f in $files) {{ [pscustomobject]@{{ Full = $f.FullName; Rel = $f.FullName.Substring($base.Length + 1) }} }}
    }}
    function Copy-Code($from, $to) {{
        foreach ($f in @(Code-Files $from)) {{
            $dst = Join-Path $to $f.Rel
            New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
            Copy-Item -LiteralPath $f.Full $dst -Force
        }}
    }}
    function Stop-Schedule {{
        foreach ($t in @(Get-ScheduledTask -TaskName "AWER_WB_*" -ErrorAction SilentlyContinue)) {{
            Disable-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath | Out-Null
        }}
        $pattern = '(wb_|direct_|metrika_|webmaster_|yandex_|crm_)\w*\.py'
        $deadline = (Get-Date).AddMinutes($WaitMin)
        while ($true) {{
            $busy = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
                Where-Object {{ $_.CommandLine -match $pattern -and $_.CommandLine -notmatch 'wb_web\.py' }})
            if (-not $busy.Count -and -not (Test-Path (Join-Path $Root "wb_bids.running"))) {{ return $true }}
            if ((Get-Date) -gt $deadline) {{ return $false }}
            Info "Дорабатывает: $(@($busy | ForEach-Object {{ ([regex]::Match($_.CommandLine, $pattern)).Value }}) -join ', ') — жду…"
            Start-Sleep -Seconds 20
        }}
    }}
    function Start-Schedule {{
        return ((Run "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "wb_schedule.ps1"), "-Server")) -eq 0)
    }}
    function Test-Load {{
        if ((Run $venvPy @("-m", "compileall", "-q", "-l", $Root)) -ne 0) {{ return $false }}
        return ((Run $venvPy @("-c", "import wb_bids, wb_guard, wb_common, wb_collector, wb_budget, wb_dashboard, wb_web, yandex_token")) -eq 0)
    }}

    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {{ Fail "Запустите PowerShell от администратора — ничего не тронуто." }}
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {{ Fail "Нет $venvPy — это не сервер со скриптами рекламы (deploy\install_server.ps1)" }}

    # --- 1. Патч цел? ---
    $parts = @({parts})
    $want = @({want})
    for ($i = 0; $i -lt $want.Count; $i++) {{
        if (-not $parts[$i] -or (Hx $parts[$i]) -ne $want[$i]) {{ Fail "Блок $($i + 1) не тот или не вставлен — вставьте его заново. Ничего не тронуто." }}
    }}
    $b = -join $parts
    if ((Hx $b) -ne "{full_sha}") {{ Fail "Патч повреждён — ничего не тронуто" }}
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $ds = New-Object IO.Compression.DeflateStream((New-Object IO.MemoryStream(,[Convert]::FromBase64String($b))), [IO.Compression.CompressionMode]::Decompress)
    $ms = New-Object IO.MemoryStream
    $ds.CopyTo($ms); $ds.Dispose()
    $apply = Join-Path $work "{tag}_apply.py"
    [IO.File]::WriteAllBytes($apply, $ms.ToArray())
    $h = [System.Security.Cryptography.SHA256]::Create()
    if ((($h.ComputeHash($ms.ToArray()) | ForEach-Object {{ $_.ToString("x2") }}) -join "") -ne "{apply_sha}") {{ Fail "Патч распаковался не так — ничего не тронуто" }}
    Ok "Патч {tag} цел (версия {version})"

    # --- 2. Проверка без изменений ---
    Info "Сверяю скрипты на сервере"
    if ((Run $venvPy @($apply, "check", $Root)) -ne 0) {{
        Fail "Ничего не тронуто: скрипты на сервере не той версии, под которую собран патч (список выше). Пришлите этот список — соберу патч под них."
    }}

    # --- 3. Пауза расписания ---
    Info "Выключаю расписание и жду, пока доработают скрипты"
    if (-not (Stop-Schedule)) {{ Start-Schedule | Out-Null; Fail "Скрипты работают дольше $WaitMin мин — патч не поставлен, расписание включено обратно. Запустите позже." }}

    # --- 4. Копия прежнего кода и запись ---
    $bk = Join-Path $Root ("code_backups\" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    Copy-Code $Root $bk
    Ok "Прежний код — в $bk"
    $wrote = (Run $venvPy @($apply, "write", $Root)) -eq 0
    if (-not $wrote -or -not (Test-Load)) {{
        Warn "Новые скрипты не записались или не загружаются — возвращаю прежний код"
        Copy-Code $bk $Root
        Start-Schedule | Out-Null
        Fail "Патч отменён, работает прежняя версия"
    }}
    Ok "Скрипты загружаются"

    # --- 5. Расписание, ярлык ---
    if (-not (Start-Schedule)) {{ Fail "Файлы стоят, но расписание не поставилось — поставьте: powershell -ExecutionPolicy Bypass -File C:\direct\wb_schedule.ps1 -Server" }}
    $sc = Join-Path $Root "deploy\shortcuts.ps1"
    if (Test-Path $sc) {{ Run "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $sc, "-Root", $Root) | Out-Null }}
    Get-ChildItem -LiteralPath (Join-Path $Root "code_backups") -Directory | Sort-Object Name | Select-Object -SkipLast 10 | Remove-Item -Recurse -Force
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    Ok "Патч {tag} поставлен: версия {version}, расписание включено"
    Write-Host "Страница «Реклама WB» была открыта — закройте вкладку и откройте ярлык заново." -ForegroundColor Cyan
}}
try {{ Invoke-{Tag} }} catch {{ Write-Host "[X] $($_.Exception.Message)" -ForegroundColor Red }}
'''


def git(*a):
    return subprocess.run(["git", "-C", HERE] + list(a), check=True, capture_output=True).stdout.decode("utf-8")


def code_set():
    out = []
    for rel in git("ls-files", "-z", ".").split("\0"):
        if not rel:
            continue
        top = "/" not in rel
        if (top and (rel.endswith(CODE_EXT) or rel == "requirements.txt")) or rel.startswith(("deploy/", "tools/")):
            out.append(rel)
    return sorted(out)


def build(base, tag, name, also=()):
    if git("status", "--porcelain", "--", ".").strip():
        sys.exit("В direct/ есть незакоммиченные изменения — сначала коммит (патч собирается из того, что в git).")
    code = code_set()
    changed = {r[len("direct/"):] for r in git("diff", "--name-only", "-z", f"{base}..HEAD", "--", ".").split("\0") if r}
    take = sorted((set(code) & changed) | set(also))
    for rel in take:
        if rel not in code:
            sys.exit(f"{rel} — не код из git (нет в списке файлов обновления)")
    version = git("log", "-1", "--format=%h %cd", "--date=format:%d.%m.%Y %H:%M").strip()
    files, keep = [], []
    for rel in code:
        with open(os.path.join(HERE, rel), "rb") as f:
            data = f.read()
        if rel in take:
            try:                    # текст — как есть (сжимается втрое лучше, чем base64 внутри base64)
                text = data.decode("utf-8")
                kind, body = ("t", text) if text.encode("utf-8") == data else ("b", None)
            except UnicodeDecodeError:
                kind = "b"
            if kind == "b":
                body = base64.b64encode(data).decode("ascii")
            files.append((rel, hashlib.sha256(data).hexdigest(), kind, body))
        else:
            keep.append((rel, hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()))
    apply_src = APPLY.format(tag=tag, version=version, files=files, keep=keep).encode("utf-8")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)            # «сырой» deflate — его распаковывает .NET DeflateStream
    b64 = base64.b64encode(c.compress(apply_src) + c.flush()).decode("ascii")
    pieces = [b64[i:i + BLOCK] for i in range(0, len(b64), BLOCK)]
    shas = [hashlib.sha256(p.encode("ascii")).hexdigest() for p in pieces]
    out = [PS_HEAD.format(tag=tag, version=version, name=name, n=len(pieces), files_list=", ".join(take))]
    for i, (piece, s) in enumerate(zip(pieces, shas), 1):
        body = "\n".join(piece[j:j + WIDTH] for j in range(0, len(piece), WIDTH))
        out.append(PS_PART.format(tag=tag, i=i, n=len(pieces), body=body, sha=s))
    out.append(PS_APPLY.format(
        tag=tag, Tag=tag.capitalize(), version=version,
        parts=", ".join(f"${tag}_{i}" for i in range(1, len(pieces) + 1)),
        want=", ".join(f'"{s}"' for s in shas),
        full_sha=hashlib.sha256(b64.encode("ascii")).hexdigest(),
        apply_sha=hashlib.sha256(apply_src).hexdigest()))
    info = {"version": version, "files": take, "keep": len(keep), "apply_bytes": len(apply_src),
            "b64": len(b64), "blocks": len(pieces)}
    return "".join(out), info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="коммит, который стоит на сервере (с него считаются изменения)")
    ap.add_argument("--tag", required=True, help="метка патча, напр. dp01")
    ap.add_argument("--out", required=True)
    ap.add_argument("--also", nargs="*", default=[], help="привезти и эти файлы (путь от direct/), даже если не менялись")
    a = ap.parse_args()
    text, info = build(a.base, a.tag, os.path.basename(a.out), a.also)
    with open(a.out, "w", encoding="utf-8-sig", newline="\r\n") as f:     # BOM: PowerShell 5.1 читает кириллицу верно
        f.write(text)
    print(info)


if __name__ == "__main__":
    main()
