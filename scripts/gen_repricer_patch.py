"""Патч для офисного ПК с репрайсером (C:\\repricer): весь каталог `repricer/` одним файлом.

Механика та же, что у `gen_whole_file_patch.py`, но патч запускается ФАЙЛОМ, а не
вставкой блоков: внутри один base64 (zlib) с apply-скриптом на python. Apply-скрипт
  - сверяет sha256 каждого файла: совпадает → `skip`;
  - иначе кладёт прежний рядом как `<файл>.bak_<tag>` и пишет новый → `ok`/`new`,
    и тут же перечитывает и сверяет;
  - пишет `deploy/INSTALLED_TAG` и печатает `DONE N`.
Берётся ВЕСЬ отслеживаемый каталог, а не разница с прошлой версией: какой именно
архив лежит на ПК, точно не известно, а лишний файл с тем же sha просто `skip`.
.env, база, ключи, копии и логи в git не лежат — патч их не трогает.

    python scripts/gen_repricer_patch.py --tag rp2 --out /tmp/rp2_patch.ps1
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import subprocess
import zlib
from pathlib import Path

APPLY = r'''import base64, datetime, hashlib, io, os, shutil, sys
ROOT = sys.argv[1] if len(sys.argv) > 1 else r"C:\repricer"
TAG = {tag!r}
FILES = {files!r}
KNOWN = {known!r}

def sha(b):
    return hashlib.sha256(b).hexdigest()

if not os.path.isfile(os.path.join(ROOT, "priceapp", "__init__.py")):
    sys.exit("Не похоже на каталог репрайсера: " + ROOT)

# Файл, правленный НА МЕСТЕ (его версии нет ни в одном релизе), патч не трогает:
# rp5 молча заменил 20 таких файлов, и программа на ПК перестала запускаться.
# Сначала проверяем ВСЁ, и при первом же чужом файле не пишем ничего.
FORCE = "--force" in sys.argv
foreign = []
for rel, expect, b64 in FILES:
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    if os.path.exists(path):
        with io.open(path, "rb") as f:
            cur = sha(f.read())
        if cur != expect and cur not in KNOWN.get(rel, ()):
            foreign.append(rel)
if foreign and not FORCE:
    print("ПАТЧ НЕ ПРИМЕНЁН: эти файлы на ПК изменены на месте, их версии нет в репозитории:")
    for rel in foreign:
        print("  " + rel.encode("ascii", "replace").decode())
    print("Замена стёрла бы эти правки. Сначала перенесите их в репозиторий.")
    sys.exit(2)
written = 0
for rel, expect, b64 in FILES:
    data = base64.b64decode(b64)
    assert sha(data) == expect, "payload corrupted: " + rel
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    if os.path.exists(path):
        with io.open(path, "rb") as f:
            if sha(f.read()) == expect:
                continue
        shutil.copy2(path, path + ".bak_" + TAG)
        status = "ok "
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        status = "new"
    with io.open(path, "wb") as f:
        f.write(data)
    with io.open(path, "rb") as f:
        assert sha(f.read()) == expect, "write verify failed: " + rel
    print(status, rel.encode("ascii", "replace").decode())
    written += 1
marker = os.path.join(ROOT, "deploy", "INSTALLED_TAG")
with io.open(marker, "w", encoding="utf-8") as f:
    f.write(TAG + "\n" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC\n")
print("tag", TAG)
print("DONE", written, "of", len(FILES))
'''

PS = r'''# ===== {tag}: патч репрайсера для C:\repricer =====
# Запуск файлом:
#   powershell -ExecutionPolicy Bypass -File C:\repricer\{tag}_patch.ps1
# Потом:
#   powershell -ExecutionPolicy Bypass -File C:\repricer\deploy\update_workstation.ps1
param([string]$Root = "C:\repricer", [switch]$Force)
$ErrorActionPreference = "Stop"
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {{ Write-Host "Нет $py — сначала install_workstation.ps1" -ForegroundColor Red; exit 1 }}
$b = @'
{body}
'@ -replace "\s",""
$h = [System.Security.Cryptography.SHA256]::Create()
$got = ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($b)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
if ($got -ne "{full_sha}") {{ Write-Host "Файл патча повреждён (sha не сходится)" -ForegroundColor Red; exit 1 }}
$b64 = Join-Path $Root "{tag}.b64"
$apply = Join-Path $Root "{tag}_apply.py"
[IO.File]::WriteAllText($b64, $b, [Text.Encoding]::ASCII)
& $py -c "import base64,sys,zlib;open(sys.argv[2],'wb').write(zlib.decompress(base64.b64decode(open(sys.argv[1]).read())))" $b64 $apply
if ($LASTEXITCODE -ne 0) {{ Write-Host "Не удалось распаковать" -ForegroundColor Red; exit 1 }}
if ($Force) {{ & $py $apply $Root --force }} else {{ & $py $apply $Root }}
if ($LASTEXITCODE -ne 0) {{ Write-Host "Патч НЕ применён — файлы не изменены" -ForegroundColor Red; Remove-Item $b64, $apply -ErrorAction SilentlyContinue; exit 1 }}
Remove-Item $b64, $apply -ErrorAction SilentlyContinue
Write-Host "Патч {tag} применён. Теперь: на сервере deploy\update_server.ps1 (от администратора), на офисном компьютере deploy\update_workstation.ps1" -ForegroundColor Green
'''


def known_versions(repo: Path, rels: list[str]) -> dict[str, list[str]]:
    """sha256 всех версий каждого файла, когда-либо бывших в репозитории: файл на
    ПК с такой версией — просто старый релиз, его заменять можно."""
    out: dict[str, list[str]] = {}
    for rel in rels:
        path = "repricer/" + rel
        commits = subprocess.run(["git", "-c", "core.quotepath=off", "log", "--format=%H", "--", path],
                                 cwd=repo, capture_output=True, check=True).stdout.decode().split()
        shas = set()
        for c in commits:
            blob = subprocess.run(["git", "show", f"{c}:{path}"], cwd=repo, capture_output=True)
            if blob.returncode == 0:
                shas.add(hashlib.sha256(blob.stdout).hexdigest())
        out[rel] = sorted(shas)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    repo = Path(__file__).resolve().parent.parent
    # Патч собирается из КОММИТА, а не из рабочей папки: иначе на боевой сервер уехал бы
    # непроверенный, возможно недописанный код, а следующий патч отказал бы по этим файлам
    # как «изменены на месте» (аудит 14.10).
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "repricer"], cwd=repo, check=True,
                           capture_output=True).stdout.decode("utf-8").strip()
    if dirty:
        raise SystemExit("В repricer/ есть незакоммиченные изменения — сначала коммит:\n" + dirty)
    names = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files", "-z", "repricer"],
                           cwd=repo, check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    files = []
    for name in sorted(n for n in names if n):
        data = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=repo, check=True, capture_output=True).stdout
        files.append((name[len("repricer/"):], hashlib.sha256(data).hexdigest(),
                      base64.b64encode(data).decode("ascii")))
    src = APPLY.format(tag=args.tag, files=files, known=known_versions(repo, [f[0] for f in files])).encode("utf-8")
    b64 = base64.b64encode(zlib.compress(src, 9)).decode("ascii")
    body = "\n".join(b64[i:i + 100] for i in range(0, len(b64), 100))
    ps = PS.format(tag=args.tag, body=body, full_sha=hashlib.sha256(b64.encode("ascii")).hexdigest())
    Path(args.out).write_text(ps, encoding="utf-8-sig", newline="\r\n")
    print(f"{args.out}: {len(files)} файлов, {len(ps) // 1024} КБ")


if __name__ == "__main__":
    main()
