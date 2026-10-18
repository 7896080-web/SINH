"""Пакет программы для сервера: один .ps1, который раскладывает файлы в C:\\mpfinance.

Механика — как у whole-file патчей sync_admin (`scripts/gen_whole_file_patch.py`
в корне репозитория): файлы релиза собираются в apply-скрипт на Python, он
сжимается zlib и кодируется base64, PowerShell сверяет SHA256 и запускает его.
Apply-скрипт:
  - файл с тем же SHA256 не трогает (`skip`), изменённый сохраняет рядом как
    `<файл>.bak_<метка>` и пишет новый (`ok`), отсутствующий создаёт (`new`);
  - в конце пишет deploy\\INSTALLED_TAG — по нему `update_server.ps1 -Tag`
    проверяет, что накатывают именно эту версию.

Первая установка: Python берётся из venv sync_admin (он на сервере есть), потом
`deploy\\install_server.ps1` заводит свой venv. Дальше пакеты кладутся поверх и
запускается `deploy\\update_server.ps1 -Tag <метка>`.

    python scripts/gen_package.py --tag fin1 --out /tmp/mpfinance_fin1.ps1

Патч (`--since`): только файлы, изменённые после версии, что стоит на сервере.
Файл `finN_deploy.ps1` кладут в папку программы и запускают (как патчи
sync_admin). Apply-скрипт патча ДО первой записи сверяет версию на диске
(`deploy\\INSTALLED_TAG` — та, на которую патч рассчитан) и SHA256 каждого
файла, который собирается заменить: файл обязан быть либо прежней версии, либо
уже новой. Разошёлся хоть один — не трогается ничего. С `--paste` тот же патч
выходит блоками для вставки в консоль (БЛОК 1 … БЛОК N, потом БЛОК APPLY).

    python scripts/gen_package.py --tag fin8 --since c0d0a1c --base-tag fin5 --out /tmp/fin8_deploy.ps1
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import lzma
import os
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Что не везём: локальное и образцы.
SKIP = (".gitignore", "deploy/INSTALLED_TAG")

APPLY_TEMPLATE = r'''import base64, datetime, hashlib, io, os, shutil, sys
ROOT = {root!r}
TAG = {tag!r}
FILES = {files!r}

def sha(b):
    return hashlib.sha256(b).hexdigest()

if os.path.normcase(os.path.abspath(ROOT)).rstrip("\\/").endswith("sync_admin"):
    sys.exit("ROOT указывает на каталог sync_admin — программа ставится в свой каталог")
written = 0
for rel, expect, b64 in FILES:
    data = base64.b64decode(b64)
    assert sha(data) == expect, "payload corrupted: " + rel
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    if os.path.exists(path):
        with io.open(path, "rb") as f:
            if sha(f.read()) == expect:
                print("skip", rel)
                continue
        shutil.copy2(path, path + ".bak_" + TAG)
        status = "ok"
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        status = "new"
    with io.open(path, "wb") as f:
        f.write(data)
    with io.open(path, "rb") as f:
        assert sha(f.read()) == expect, "write verify failed: " + rel
    print(status, rel)
    written += 1
marker = os.path.join(ROOT, "deploy", "INSTALLED_TAG")
os.makedirs(os.path.dirname(marker), exist_ok=True)
with io.open(marker, "w", encoding="utf-8") as f:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    f.write(TAG + "\n" + stamp + " UTC\n")
print("tag", TAG)
print("DONE", written)
'''

PS_TEMPLATE = r'''# ===== {tag}: пакет программы «Финансы площадок» для {root} =====
# Запуск (PowerShell от администратора):
#     powershell -ExecutionPolicy Bypass -File <путь к этому файлу>
# Пакет только раскладывает файлы. Дальше:
#   первая установка  — powershell -ExecutionPolicy Bypass -File {root}\deploy\install_server.ps1
#   обновление        — powershell -ExecutionPolicy Bypass -File {root}\deploy\update_server.ps1 -Tag {tag}
$ErrorActionPreference = "Stop"
$Root = "{root}"
function Hx($s) {{
    $h = [System.Security.Cryptography.SHA256]::Create()
    ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
}}
$b = @'
{body}
'@
$b = $b -replace "\s", ""
if ((Hx $b) -ne "{full_sha}") {{ Write-Host "[X] Пакет повреждён (SHA256 не сошёлся) — скачайте файл заново" -ForegroundColor Red; exit 1 }}
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$py = @("$Root\.venv\Scripts\python.exe", "C:\sync_admin\.venv\Scripts\python.exe") | Where-Object {{ Test-Path $_ }} | Select-Object -First 1
if (-not $py) {{
    foreach ($c in @("py", "python")) {{ if (Get-Command $c -ErrorAction SilentlyContinue) {{ $py = (Get-Command $c).Source; break }} }}
}}
if (-not $py) {{ Write-Host "[X] Не найден Python (ни venv программы, ни venv sync_admin, ни в PATH)" -ForegroundColor Red; exit 1 }}
[IO.File]::WriteAllText("$Root\{tag}.b64", $b, [Text.Encoding]::ASCII)
& $py -c "import base64,zlib,sys;open(sys.argv[2],'wb').write(zlib.decompress(base64.b64decode(open(sys.argv[1]).read())))" "$Root\{tag}.b64" "$Root\{tag}_apply.py"
& $py "$Root\{tag}_apply.py"
if ($LASTEXITCODE -ne 0) {{ Write-Host "[X] Файлы не разложены — см. вывод выше" -ForegroundColor Red; exit 1 }}
Remove-Item "$Root\{tag}.b64", "$Root\{tag}_apply.py" -Force
Write-Host "[OK] Пакет {tag} разложен в $Root" -ForegroundColor Green
if (Test-Path "$Root\.venv\Scripts\python.exe") {{
    Write-Host "Дальше: powershell -ExecutionPolicy Bypass -File $Root\deploy\update_server.ps1 -Tag {tag}"
}} else {{
    Write-Host "Дальше (первая установка): powershell -ExecutionPolicy Bypass -File $Root\deploy\install_server.ps1"
}}
'''


PATCH_APPLY_TEMPLATE = r'''import base64, datetime, hashlib, io, os, shutil, sys
ROOT = {root!r}
TAG = {tag!r}
BASE_TAG = {base_tag!r}
# (путь, sha нового, sha прежнего или None — файла не было, "t"/"b", содержимое)
FILES = {files!r}

def sha(b):
    return hashlib.sha256(b).hexdigest()

def payload(kind, body):
    return body.encode("utf-8") if kind == "t" else base64.b64decode(body)

marker = os.path.join(ROOT, "deploy", "INSTALLED_TAG")
installed = ""
if os.path.exists(marker):
    with io.open(marker, encoding="utf-8") as f:
        installed = (f.readline() or "").strip()
if installed not in (BASE_TAG, TAG):
    sys.exit("На диске версия '%s', а патч %s рассчитан на %s — ничего не тронуто. "
             "Нужен полный пакет." % (installed or "неизвестна", TAG, BASE_TAG))

# Сначала сверяем ВСЁ, потом пишем: файл, правленный руками или другой
# версии, патч не перезаписывает — и не начинает, если такой есть.
plan, wrong = [], []
for rel, expect, before, kind, body in FILES:
    data = payload(kind, body)
    assert sha(data) == expect, "payload corrupted: " + rel
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    current = None
    if os.path.exists(path):
        with io.open(path, "rb") as f:
            current = sha(f.read())
    if current == expect:
        print("skip", rel)
        continue
    if current != before:
        wrong.append(rel + (" (нет файла)" if current is None else " (не та версия)"))
        continue
    plan.append((rel, path, expect, data, current is not None))
if wrong:
    print("Не сходятся с версией %s:" % BASE_TAG)
    for item in wrong:
        print("   ", item)
    sys.exit("Ничего не тронуто.")

written = 0
for rel, path, expect, data, existed in plan:
    if existed:
        shutil.copy2(path, path + ".bak_" + TAG)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as f:
        f.write(data)
    with io.open(path, "rb") as f:
        assert sha(f.read()) == expect, "write verify failed: " + rel
    print("ok" if existed else "new", rel)
    written += 1
with io.open(marker, "w", encoding="utf-8") as f:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    f.write(TAG + "\n" + stamp + " UTC\n")
print("tag", TAG)
print("DONE", written)
'''

PASTE_PART = r'''
# ---- БЛОК {i} из {n} ----
function Hx($s) {{ $h = [System.Security.Cryptography.SHA256]::Create(); ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join "" }}
${var}{i} = @'
{body}
'@
${var}{i} = ${var}{i} -replace "\s",""
"{var}{i} got    " + (Hx ${var}{i})
"{var}{i} expect {sha}"
'''

PASTE_APPLY = r'''
# ---- БЛОК APPLY ----
function Hx($s) {{ $h = [System.Security.Cryptography.SHA256]::Create(); ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join "" }}
{part_checks}
$b = {concat}
"full got    " + (Hx $b)
"full expect {full_sha}"
if ((Hx $b) -ne "{full_sha}") {{
    Write-Host "[X] Не сошлось — ничего не тронуто. Вставьте заново блоки с пометкой НЕ ТОТ." -ForegroundColor Red
}} else {{
    $py = "{root}\.venv\Scripts\python.exe"
    [IO.File]::WriteAllText("{root}\{tag}.b64", $b, [Text.Encoding]::ASCII)
    & $py -c "import base64,lzma,sys;open(sys.argv[2],'wb').write(lzma.decompress(base64.b64decode(open(sys.argv[1]).read())))" "{root}\{tag}.b64" "{root}\{tag}_apply.py"
    & $py "{root}\{tag}_apply.py"
    if ($LASTEXITCODE -eq 0) {{
        Remove-Item "{root}\{tag}.b64", "{root}\{tag}_apply.py" -Force
        Write-Host "[OK] Патч {tag} разложен. Дальше:" -ForegroundColor Green
        Write-Host "powershell -ExecutionPolicy Bypass -File {root}\deploy\update_server.ps1 -Tag {tag}"
    }} else {{
        Write-Host "[X] Патч не применён — см. вывод выше" -ForegroundColor Red
    }}
}}
'''


PATCH_FILE = r'''# ===== {tag}: патч программы «Финансы площадок» для {root} — поверх {base_tag} =====
# Положить в {root} и запустить (PowerShell от администратора):
#     powershell -ExecutionPolicy Bypass -File {root}\{tag}_deploy.ps1
#     powershell -ExecutionPolicy Bypass -File {root}\deploy\update_server.ps1 -Tag {tag}
# Файлов в патче: {count}. До первой записи сверяются версия на диске ({base_tag}) и
# SHA256 каждого заменяемого файла; разошлось хоть что-то — не трогается ничего.
$ErrorActionPreference = "Stop"
$Root = "{root}"
function Hx($s) {{
    $h = [System.Security.Cryptography.SHA256]::Create()
    ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
}}
$b = @'
{body}
'@
$b = $b -replace "\s", ""
if ((Hx $b) -ne "{full_sha}") {{ Write-Host "[X] Патч повреждён (SHA256 не сошёлся) — перенесите файл заново" -ForegroundColor Red; exit 1 }}
$py = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {{ Write-Host "[X] Нет $py — программа не установлена (сначала deploy\install_server.ps1)" -ForegroundColor Red; exit 1 }}
[IO.File]::WriteAllText("$Root\{tag}.b64", $b, [Text.Encoding]::ASCII)
& $py -c "import base64,lzma,sys;open(sys.argv[2],'wb').write(lzma.decompress(base64.b64decode(open(sys.argv[1]).read())))" "$Root\{tag}.b64" "$Root\{tag}_apply.py"
& $py "$Root\{tag}_apply.py"
$rc = $LASTEXITCODE
Remove-Item "$Root\{tag}.b64", "$Root\{tag}_apply.py" -Force -ErrorAction SilentlyContinue
if ($rc -ne 0) {{ Write-Host "[X] Патч не применён — см. вывод выше" -ForegroundColor Red; exit 1 }}
Write-Host "[OK] Патч {tag} разложен в $Root" -ForegroundColor Green
Write-Host "Дальше: powershell -ExecutionPolicy Bypass -File $Root\deploy\update_server.ps1 -Tag {tag}"
'''


def _git_bytes(root: str, ref: str, rel: str) -> bytes | None:
    """Файл в версии `ref` (путь — от корня программы) или None, если его там нет."""
    done = subprocess.run(["git", "-C", root, "show", f"{ref}:./{rel}"], capture_output=True)
    return done.stdout if done.returncode == 0 else None


def changed_since(root: str, ref: str) -> tuple[list[tuple[str, bytes, bytes | None]], list[str]]:
    """Файлы релиза, изменённые или добавленные после `ref`: (путь, новое,
    прежнее или None), и файлы, которых в релизе больше нет."""
    files = release_files(root)
    changed = []
    for rel in files:
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
        before = _git_bytes(root, ref, rel)
        if before != data:
            changed.append((rel, data, before))
    # -z: без него git экранирует кириллицу в путях, и файл «АУДИТ_….md»
    # выглядел бы удалённым.
    old = subprocess.run(["git", "-C", root, "ls-tree", "-r", "-z", "--name-only", ref, "."],
                         check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    gone = sorted(p for p in old if p and p not in files and p not in SKIP)
    return changed, gone


def build_patch(root: str, tag: str, since: str, base_tag: str, target: str,
                parts: int = 3, width: int = 80, var: str = "m", paste: bool = True) -> tuple[str, dict]:
    changed, gone = changed_since(root, since)
    if gone:
        # Патч только пишет. Удалённый файл остался бы на сервере, и тест,
        # берущий каталог глобом, увидел бы там не релиз, а осадок.
        raise SystemExit("после %s удалены файлы — патчем это не накатить, нужен полный пакет: %s"
                         % (since, ", ".join(gone)))
    entries = []
    for rel, data, before in changed:
        try:
            kind, body = "t", data.decode("utf-8")
        except UnicodeDecodeError:
            kind, body = "b", base64.b64encode(data).decode("ascii")
        entries.append((rel, hashlib.sha256(data).hexdigest(),
                        hashlib.sha256(before).hexdigest() if before is not None else None, kind, body))
    apply_src = PATCH_APPLY_TEMPLATE.format(root=target, tag=tag, base_tag=base_tag, files=entries)
    b64 = base64.b64encode(lzma.compress(apply_src.encode("utf-8"), preset=9 | lzma.PRESET_EXTREME)).decode("ascii")
    full_sha = hashlib.sha256(b64.encode("ascii")).hexdigest()
    info = {"files": [e[0] for e in entries], "b64_len": len(b64), "full_sha": full_sha}
    if not paste:
        body = "\n".join(b64[i:i + 100] for i in range(0, len(b64), 100))
        return PATCH_FILE.format(tag=tag, root=target, base_tag=base_tag, count=len(entries), body=body,
                                 full_sha=full_sha), info
    chunk = (len(b64) + parts - 1) // parts
    pieces = [b64[i:i + chunk] for i in range(0, len(b64), chunk)]
    out = [f"# ===== {tag}: патч «Финансов площадок» для {target} — поверх {base_tag} =====\n"
           f"# Вставлять в PowerShell НА СЕРВЕРЕ (от администратора) по очереди: БЛОК 1 … БЛОК "
           f"{len(pieces)}, потом БЛОК APPLY.\n"
           f"# После каждого блока строки got и expect должны совпасть.\n"]
    for i, piece in enumerate(pieces, 1):
        body = "\n".join(piece[j:j + width] for j in range(0, len(piece), width))
        out.append(PASTE_PART.format(i=i, n=len(pieces), body=body, var=var,
                                     sha=hashlib.sha256(piece.encode("ascii")).hexdigest()))
    part_checks = "\n".join(
        f'if ((Hx ${var}{i}) -eq "{hashlib.sha256(piece.encode("ascii")).hexdigest()}") '
        f'{{ "{var}{i} OK" }} else {{ "{var}{i} НЕ ТОТ — вставьте блок {i} заново" }}'
        for i, piece in enumerate(pieces, 1))
    concat = " + ".join(f"${var}{i}" for i in range(1, len(pieces) + 1))
    out.append(PASTE_APPLY.format(part_checks=part_checks, concat=concat, full_sha=full_sha,
                                  root=target, tag=tag))
    return "".join(out), dict(info, parts=len(pieces))


def release_files(root: str) -> list[str]:
    # --others --exclude-standard: и новые, ещё не добавленные в git файлы (но не
    # то, что .gitignore исключает, — базы, .env, кэш).
    out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard", "--", "."], check=True,
                         capture_output=True).stdout.decode("utf-8")
    files = [p for p in out.split("\0") if p]
    # Удалённый с диска, но ещё не закоммиченный файл git по-прежнему числит в
    # индексе: в релиз его не везём — его больше нет.
    return sorted(p for p in files if p not in SKIP and "__pycache__" not in p
                  and os.path.isfile(os.path.join(root, p)))


def build(root: str, tag: str, target: str, width: int = 100) -> tuple[str, dict]:
    entries = []
    for rel in release_files(root):
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
        entries.append((rel, hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode("ascii")))
    apply_src = APPLY_TEMPLATE.format(root=target, tag=tag, files=entries).encode("utf-8")
    b64 = base64.b64encode(zlib.compress(apply_src, 9)).decode("ascii")
    body = "\n".join(b64[i:i + width] for i in range(0, len(b64), width))
    text = PS_TEMPLATE.format(tag=tag, root=target, body=body,
                              full_sha=hashlib.sha256(b64.encode("ascii")).hexdigest())
    return text, {"files": len(entries), "b64_len": len(b64)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True, help="метка пакета, напр. fin1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", default=r"C:\mpfinance")
    ap.add_argument("--since", help="патч: только изменённое после этой версии git")
    ap.add_argument("--base-tag", help="метка версии на сервере, на которую рассчитан патч (с --since)")
    ap.add_argument("--paste", action="store_true", help="патч блоками для вставки в консоль, а не файлом")
    ap.add_argument("--parts", type=int, default=3, help="сколько блоков base64 (с --paste)")
    a = ap.parse_args()
    if a.since:
        if not a.base_tag:
            ap.error("с --since нужен --base-tag — какая версия стоит на сервере")
        text, info = build_patch(HERE, a.tag, a.since, a.base_tag, a.target, parts=a.parts, paste=a.paste)
    else:
        text, info = build(HERE, a.tag, a.target)
    # С BOM: Windows PowerShell 5.1 читает .ps1 без BOM как cp1251, и кириллица
    # (а с ней и кавычки строк) ломается — урок патчей sync_admin 21.09.
    with open(a.out, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    print(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
