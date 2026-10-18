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
"""
from __future__ import annotations

import argparse
import base64
import hashlib
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


def release_files(root: str) -> list[str]:
    # --others --exclude-standard: и новые, ещё не добавленные в git файлы (но не
    # то, что .gitignore исключает, — базы, .env, кэш).
    out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard", "--", "."], check=True,
                         capture_output=True).stdout.decode("utf-8")
    files = [p for p in out.split("\0") if p]
    return sorted(p for p in files if p not in SKIP and "__pycache__" not in p)


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
    a = ap.parse_args()
    text, info = build(HERE, a.tag, a.target)
    # С BOM: Windows PowerShell 5.1 читает .ps1 без BOM как cp1251, и кириллица
    # (а с ней и кавычки строк) ломается — урок патчей sync_admin 21.09.
    with open(a.out, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    print(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
