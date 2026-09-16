"""Генератор пробы различий: сравнить файлы на сервере с версией из репозитория.

Направление «сервер → чат» для больших выгрузок ненадёжно (консоль портит отдельные
символы в base64, а длина при этом сходится — zlib падает уже при разборе). Поэтому
эталон едет НА сервер тем же проверенным каналом, что и патчи (zlib+base64 с SHA256),
а сервер печатает готовый unified diff — обычно это десяток строк, которые можно
вставить в чат без риска.

Ничего не меняет: файлы сервера только читаются, эталон разворачивается в память.

    python scripts/gen_diff_probe.py --out /tmp/probe.ps1 1c/Модуль.txt deploy/install_windows.ps1
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
import zlib

PROBE_TEMPLATE = r'''import base64, difflib, io, os, zlib
ROOT = r"C:\sync_admin"
BLOB = {blob!r}
REPO = {{}}
for chunk in zlib.decompress(base64.b64decode(BLOB)).split(b"\x00\x00SEP\x00\x00"):
    name, data = chunk.split(b"\x01", 1)
    REPO[name.decode("utf-8")] = data

for rel, repo_bytes in REPO.items():
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    print("=" * 70)
    if not os.path.exists(path):
        print("НЕТ НА СЕРВЕРЕ:", rel)
        continue
    srv_bytes = io.open(path, "rb").read()
    if srv_bytes == repo_bytes:
        print("СОВПАДАЮТ:", rel)
        continue
    print("ОТЛИЧАЮТСЯ:", rel, "| сервер", len(srv_bytes), "б, репозиторий", len(repo_bytes), "б")
    a = repo_bytes.decode("utf-8", "replace").splitlines()
    b = srv_bytes.decode("utf-8", "replace").splitlines()
    diff = list(difflib.unified_diff(a, b, "репозиторий", "СЕРВЕР", lineterm="", n=2))
    print("строк в диффе:", len(diff))
    for line in diff[:400]:
        print(line)
    if len(diff) > 400:
        print("... обрезано, показаны первые 400 строк")
print("=" * 70)
print("--- END PROBE ---")
'''

PS_TEMPLATE = r'''# ===== Проба различий: {names} (только чтение) =====
function Hx($s) {{
    $h = [System.Security.Cryptography.SHA256]::Create()
    ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
}}
$p = @'
{body}
'@
$p = $p -replace "\s",""
"payload got    " + (Hx $p)
"payload expect {sha}"
# Если строки выше не совпали — не продолжайте, вставьте блок заново.
$py = "C:\sync_admin\.venv\Scripts\python.exe"
[IO.File]::WriteAllText("C:\sync_admin\probe.b64", $p, [Text.Encoding]::ASCII)
& $py -c "import base64,zlib;open(r'C:\sync_admin\probe.py','wb').write(zlib.decompress(base64.b64decode(open(r'C:\sync_admin\probe.b64').read())))"
& $py "C:\sync_admin\probe.py"
Remove-Item C:\sync_admin\probe.b64, C:\sync_admin\probe.py -ErrorAction SilentlyContinue
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="пути относительно корня репозитория (через /)")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--width", type=int, default=80)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    parts = []
    for rel in a.files:
        with open(os.path.join(a.root, rel), "rb") as f:
            parts.append(rel.encode("utf-8") + b"\x01" + f.read())
    blob = base64.b64encode(zlib.compress(b"\x00\x00SEP\x00\x00".join(parts), 9)).decode("ascii")

    probe_src = PROBE_TEMPLATE.format(blob=blob).encode("utf-8")
    payload = base64.b64encode(zlib.compress(probe_src, 9)).decode("ascii")
    body = "\n".join(payload[i:i + a.width] for i in range(0, len(payload), a.width))

    with open(a.out, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(PS_TEMPLATE.format(names=", ".join(a.files), body=body,
                                   sha=hashlib.sha256(payload.encode("ascii")).hexdigest()))
    print({"files": len(a.files), "payload_len": len(payload),
           "lines": len(body.splitlines())})
    return 0


if __name__ == "__main__":
    sys.exit(main())
