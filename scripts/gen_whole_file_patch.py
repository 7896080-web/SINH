"""Генератор whole-file патча для сервера (C:\\sync_admin) по установленной механике.

Собирает указанные файлы релиза в один apply-скрипт (python), сжимает zlib + base64,
режет base64 на N частей по 80 символов в строке и выдаёт готовый PowerShell-скрипт
с проверкой SHA256 каждой части и полного b64. На сервере apply-скрипт:
  - сравнивает SHA256 существующего файла с релизным → `skip` если совпадает;
  - иначе делает бэкап `<файл>.bak_<tag>` (если файл был) и пишет байты релиза → `ok`/`new`;
  - в конце печатает `DONE N` (N — записанных файлов).

Пример:
    python scripts/gen_whole_file_patch.py --tag pm12 --out /tmp/pm12_deploy.ps1 \
        app/models.py app/workers/dispatch.py ...
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
import zlib

APPLY_TEMPLATE = r'''import base64, datetime, hashlib, io, os, shutil
ROOT = r"C:\sync_admin"
TAG = {tag!r}
FILES = {files!r}
DELETE = {delete!r}

def sha(b):
    return hashlib.sha256(b).hexdigest()

written = 0
for rel, expect, b64 in FILES:
    data = base64.b64decode(b64)
    assert sha(data) == expect, "payload corrupted: " + rel
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    if os.path.exists(path):
        with io.open(path, "rb") as f:
            cur = f.read()
        if sha(cur) == expect:
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

# Удаление файлов, которых больше нет в релизе (переименование/слияние страниц).
# Сам файл уносим в бэкап, .pyc рядом — просто удаляем, иначе Python может
# импортировать устаревший байт-код, а pytest — подобрать удалённые тесты.
for rel in DELETE:
    path = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.exists(path):
        print("gone", rel)
        continue
    shutil.move(path, path + ".bak_" + TAG)
    base = os.path.basename(path)
    if base.endswith(".py"):
        cache = os.path.join(os.path.dirname(path), "__pycache__")
        if os.path.isdir(cache):
            for f in os.listdir(cache):
                if f.startswith(base[:-3] + "."):
                    os.remove(os.path.join(cache, f))
    print("del", rel)
    written += 1
# Отметка «какой блок наката тут реально применялся». Пишем ПОСЛЕ всех файлов,
# то есть только когда всё записано и сверено по sha.
#
# Зачем. Блок наката и `update_windows.ps1` — два независимых шага, и второй
# ничего не знает о первом. 23.09 это стоило вечера: APPLY не запускали вовсе,
# `update_windows.ps1` честно отработал на СТАРОМ коде и закончился зелёным —
# копия снята, тесты зелёные (код и база друг другу соответствуют), `/health`
# 200. Понять, что новой версии на сервере нет, удалось только по отсутствию
# строки `Running upgrade` в логе миграций, то есть по косвенному признаку,
# которого никто не ищет.
#
# Теперь версия названа вслух, а `update_windows.ps1 -Tag pm114` сверяет её с
# той, которую человек собирался ставить, и отказывается работать при
# расхождении.
marker = os.path.join(ROOT, "deploy", "INSTALLED_TAG")
os.makedirs(os.path.dirname(marker), exist_ok=True)
with io.open(marker, "w", encoding="utf-8") as f:
    # `now(timezone.utc)`, а не `utcnow()`: второй помечен на удаление и на
    # боевом Python печатает DeprecationWarning ПЕРВОЙ строкой наката. Строка
    # выходит та же, а предупреждение в самом начале вывода — это то, что учит
    # не читать вывод целиком; дальше там «DONE N» и метка версии, ради которых
    # его и смотрят.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    f.write(TAG + "\n" + stamp + " UTC\n")
print("tag", TAG)
print("DONE", written)
'''

PS_HEADER = r'''# ===== {tag}: whole-file патч для C:\sync_admin =====
# Вставлять в PowerShell НА СЕРВЕРЕ блоками (БЛОК 1 … БЛОК {nblocks}, потом БЛОК APPLY).
# После каждого блока сверить напечатанный SHA256 с ожидаемым.
function Hx($s) {{
    $h = [System.Security.Cryptography.SHA256]::Create()
    ($h.ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object {{ $_.ToString("x2") }}) -join ""
}}
'''

PS_PART = r'''
# ---- БЛОК {i} из {n} ----
$p{i} = @'
{body}
'@
$p{i} = $p{i} -replace "\s",""
"p{i} got    " + (Hx $p{i})
"p{i} expect {sha}"
'''

PS_APPLY = r'''
# ---- БЛОК APPLY ----
# Сначала сверяем КАЖДУЮ часть по отдельности. Если какой-то блок не вставили,
# в переменной остаётся кусок ПРОШЛОГО патча — полный хэш тогда не сойдётся, но
# без этой проверки непонятно, какой именно блок виноват (так и случилось с pm31:
# $p2 остался от pm30).
{part_checks}
$b = {concat}
"full got    " + (Hx $b)
"full expect {full_sha}"
[IO.File]::WriteAllText("C:\sync_admin\{tag}.b64", $b, [Text.Encoding]::ASCII)
$py = "C:\sync_admin\.venv\Scripts\python.exe"
& $py -c "import base64,zlib;open(r'C:\sync_admin\{tag}_apply.py','wb').write(zlib.decompress(base64.b64decode(open(r'C:\sync_admin\{tag}.b64').read())))"
& $py "C:\sync_admin\{tag}_apply.py"
'''


def build(root: str, files: list[str], tag: str, parts: int, width: int,
          delete: list[str] | None = None) -> tuple[str, dict]:
    entries = []
    for rel in files:
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
        entries.append((rel, hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode("ascii")))
    apply_src = APPLY_TEMPLATE.format(tag=tag, files=entries, delete=list(delete or [])).encode("utf-8")
    b64 = base64.b64encode(zlib.compress(apply_src, 9)).decode("ascii")
    full_sha = hashlib.sha256(b64.encode("ascii")).hexdigest()

    chunk = (len(b64) + parts - 1) // parts
    pieces = [b64[i:i + chunk] for i in range(0, len(b64), chunk)]
    out = [PS_HEADER.format(tag=tag, nblocks=len(pieces))]
    for i, piece in enumerate(pieces, 1):
        body = "\n".join(piece[j:j + width] for j in range(0, len(piece), width))
        out.append(PS_PART.format(i=i, n=len(pieces), body=body,
                                  sha=hashlib.sha256(piece.encode("ascii")).hexdigest()))
    concat = " + ".join(f"$p{i}" for i in range(1, len(pieces) + 1))
    part_checks = "\n".join(
        # Фигурные скобки здесь одинарные: строка подставляется в шаблон через
        # format() уже готовой, повторного форматирования не будет.
        f'if ((Hx $p{i}) -eq "{hashlib.sha256(piece.encode("ascii")).hexdigest()}") '
        f'{{ "p{i} OK" }} else {{ "p{i} НЕ ТОТ — вставьте блок {i} заново" }}'
        for i, piece in enumerate(pieces, 1)
    )
    out.append(PS_APPLY.format(concat=concat, full_sha=full_sha, tag=tag, part_checks=part_checks))
    info = {"files": len(entries), "deleted": len(delete or []),
            "raw_bytes": len(apply_src), "b64_len": len(b64),
            "parts": len(pieces), "full_sha": full_sha}
    return "".join(out), info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="пути относительно корня релиза (через /)")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--tag", required=True, help="метка патча, напр. pm12 (суффикс бэкапов .bak_<tag>)")
    ap.add_argument("--parts", type=int, default=2)
    ap.add_argument("--width", type=int, default=80)
    ap.add_argument("--delete", nargs="*", default=[],
                    help="пути, которые нужно УДАЛИТЬ на сервере (уносятся в .bak_<tag>)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    text, info = build(a.root, a.files, a.tag, a.parts, a.width, a.delete)
    # utf-8-SIG, то есть с BOM, и это не косметика. Windows PowerShell 5.1
    # считает, что .ps1 без BOM написан в системной кодировке (cp1251), и
    # кириллица в комментариях и сообщениях превращается в мусор. Беда не в
    # нечитаемости: длинное тире «—» (E2 80 94) читается как «вЂ"», и последний
    # символ закрывает строковый литерал раньше времени — парсер падает на
    # «непредвиденная лексема», не дойдя до блоков. 21.09 на бою так и вышло при
    # запуске патча ФАЙЛОМ. Вставка блоков руками в консоль это не ловила:
    # там кодировка уже верная, поэтому дефект дожил до первого запуска файлом.
    with open(a.out, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    print(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
