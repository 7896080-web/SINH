"""Обновление awer-sales-bot на сервере текстом (base64) — по механике Sync Admin (../scripts/gen_whole_file_patch.py).

Берёт файлы программы, изменённые с коммита --base (установленная версия), целиком; кладёт их с manifest.json
(SHA256 каждого файла, список удалённых) и apply.py в tar, сжимает zlib, кодирует base64 и режет на блоки по
80 символов в строке. Получается текст для PowerShell на сервере: блоки вставляются по очереди, у каждого
печатается SHA256 для сверки. Последний блок (APPLY):
  1. распаковывает пакет Python-ом программы; apply.py копирует текущий код из C:\\awer-sales-bot в
     backups\\patch-<метка>\\awer-sales-bot и кладёт поверх файлы обновления (с проверкой SHA256), удаляет лишние;
  2. запускает из этой копии scripts\\install.ps1 — обычное обновление: снимок базы, замена кода, тесты, запуск
     службы; не прошли тесты или служба не ответила — возвращается прежний код.

Пример:
    python scripts/make_patch.py --base 63f8aab --tag u3 --out /tmp/awer-update-u3.ps1.txt
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# то же, что копирует install.ps1 ($CodeDirs, $CodeFiles)
CODE = ("src/", "content/", "scripts/", "docs/", "tests/", "onec/ВыгрузкаПродажДляБота/",
        "pyproject.toml", "config.example.yaml", ".env.example", "README.md")

APPLY = r'''import hashlib, json, os, shutil, sys
APP = os.environ.get("AWER_APP", r"C:\awer-sales-bot")  # AWER_APP — только для проверки обновления у разработчика
PKG = sys.argv[1]
m = json.load(open(os.path.join(PKG, "manifest.json"), encoding="utf-8"))
stage = os.path.join(APP, "backups", "patch-" + m["tag"], "awer-sales-bot")
if os.path.exists(stage):
    shutil.rmtree(stage)
skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
for d in ("src", "content", "scripts", "docs", "tests"):
    if os.path.isdir(os.path.join(APP, d)):
        shutil.copytree(os.path.join(APP, d), os.path.join(stage, d), ignore=skip)
onec = os.path.join(APP, "onec")
for d in (os.listdir(onec) if os.path.isdir(onec) else []):  # исходники обработки (собранная .epf остаётся на месте)
    if os.path.isdir(os.path.join(onec, d)):
        shutil.copytree(os.path.join(onec, d), os.path.join(stage, "onec", d), ignore=skip)
for f in ("pyproject.toml", "config.example.yaml", ".env.example", "README.md"):
    if os.path.exists(os.path.join(APP, f)):
        shutil.copy2(os.path.join(APP, f), os.path.join(stage, f))
n = 0
for rel, expect in m["files"]:
    with open(os.path.join(PKG, "files", rel), "rb") as f:
        data = f.read()
    assert hashlib.sha256(data).hexdigest() == expect, "повреждён файл обновления: " + rel
    path = os.path.join(stage, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    n += 1
for rel in m["delete"]:
    path = os.path.join(stage, rel.replace("/", os.sep))
    if os.path.exists(path):
        os.remove(path)
        n += 1
print("файлов обновления:", n)
print("STAGED", stage)
'''

PS_HEADER = r'''# ===== awer-sales-bot, обновление {tag} =====
# PowerShell ОТ ИМЕНИ АДМИНИСТРАТОРА на сервере. Вставлять по очереди: БЛОК 1 … БЛОК {n}, потом БЛОК APPLY.
# После каждого блока сверить напечатанный SHA256 с ожидаемым (got = expect).
function Hx($s) {{
    $h = [System.Security.Cryptography.SHA256]::Create()
    ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
}}
'''

PS_PART = r'''
# ---- БЛОК {i} из {n} ----
$u{i} = @'
{body}
'@
$u{i} = $u{i} -replace "\s",""
"u{i} got    " + (Hx $u{i})
"u{i} expect {sha}"
'''

PS_APPLY = r'''
# ---- БЛОК APPLY ---- (весь блок — одна команда: при ошибке проверки дальше не идёт)
& {{
{checks}
$b = {concat}
if ((Hx $b) -ne "{full}") {{ "Обновление собрано не полностью — вставьте блоки заново"; return }}
$dir = "C:\awer-sales-bot\backups\patch-{tag}"
Remove-Item -Recurse -Force "$dir\pkg" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dir | Out-Null
[IO.File]::WriteAllText("$dir\{tag}.b64", $b, [Text.Encoding]::ASCII)
$py = "C:\awer-sales-bot\.venv\Scripts\python.exe"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
& $py -c "import base64,io,tarfile,zlib;tarfile.open(fileobj=io.BytesIO(zlib.decompress(base64.b64decode(open(r'$dir\{tag}.b64').read())))).extractall(r'$dir\pkg', filter='data')"
$out = & $py "$dir\pkg\apply.py" "$dir\pkg"
$out
$stage = ($out | Where-Object {{ $_ -like "STAGED *" }}) -replace "^STAGED ", ""
if (-not $stage) {{ "Обновление не разложено — пришлите вывод выше разработчику"; return }}
powershell -ExecutionPolicy Bypass -File "$stage\scripts\install.ps1"
}}
'''


def changed(base: str) -> tuple[list[str], list[str]]:
    def names(flt: str) -> list[str]:
        out = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--relative", "--name-only", f"--diff-filter={flt}",
                              base, "HEAD", "--", "."], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [n for n in out.splitlines() if n.startswith(CODE) or n in CODE]
    return names("ACMR"), names("D")


def package(tag: str, files: list[str], delete: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def put(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        entries = []
        for rel in files:
            data = (ROOT / rel).read_bytes()
            entries.append([rel, hashlib.sha256(data).hexdigest()])
            put("files/" + rel, data)
        put("manifest.json", json.dumps({"tag": tag, "files": entries, "delete": delete},
                                        ensure_ascii=False).encode("utf-8"))
        put("apply.py", APPLY.encode("utf-8"))
    return zlib.compress(buf.getvalue(), 9)


def build(base: str, tag: str, parts: int, width: int) -> tuple[str, dict]:
    files, delete = changed(base)
    b64 = base64.b64encode(package(tag, files, delete)).decode("ascii")
    size = (len(b64) + parts - 1) // parts
    pieces = [b64[i:i + size] for i in range(0, len(b64), size)]
    sha = lambda s: hashlib.sha256(s.encode("ascii")).hexdigest()  # noqa: E731
    out = [PS_HEADER.format(tag=tag, n=len(pieces))]
    for i, p in enumerate(pieces, 1):
        body = "\n".join(p[j:j + width] for j in range(0, len(p), width))
        out.append(PS_PART.format(i=i, n=len(pieces), body=body, sha=sha(p)))
    checks = "\n".join(f'if ((Hx $u{i}) -eq "{sha(p)}") {{ "u{i} OK" }} else {{ "u{i} НЕ ТОТ — вставьте блок {i} заново"; '
                       f'return }}' for i, p in enumerate(pieces, 1))
    concat = " + ".join(f"$u{i}" for i in range(1, len(pieces) + 1))
    out.append(PS_APPLY.format(checks=checks, concat=concat, full=sha(b64), tag=tag))
    return "".join(out), {"files": len(files), "deleted": len(delete), "b64": len(b64), "parts": len(pieces)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="коммит установленной версии")
    ap.add_argument("--tag", required=True, help="метка обновления (латиницей), например u3")
    ap.add_argument("--parts", type=int, default=2)
    ap.add_argument("--width", type=int, default=80)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=ROOT, capture_output=True, text=True).stdout:
        print("есть незакоммиченные изменения — сначала коммит", file=sys.stderr)
        return 1
    text, info = build(a.base, a.tag, a.parts, a.width)
    Path(a.out).write_text(text, encoding="utf-8", newline="\r\n")
    print(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
