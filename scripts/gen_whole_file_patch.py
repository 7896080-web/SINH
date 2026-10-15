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

# ---------------------------------------------------------------------------
# ЧТО ОБЯЗАНО ЛЕЖАТЬ НА СЕРВЕРЕ — состав релиза
# ---------------------------------------------------------------------------
# Опись отвечает на вопрос «диск сервера — это репозиторий?», и отвечать она
# должна про то, от чего зависят РАБОТА и РЕМОНТ, а не про весь репозиторий.
# Шире — хуже: опись, в которой полтора десятка строк «нет на диске» про
# материалы разработки, читается один раз, и дальше её пролистывают вместе с
# настоящей пропажей.
RELEASE_DIRS = ("app/", "alembic/", "deploy/", "scripts/", "tests/")

# Корневые файлы перечислены ПОИМЁННО, и список закрыт тестом: новый файл в
# корне не попадёт в релиз молча — тест упадёт и заставит решить, везём мы его
# или нет. Молчаливое умолчание тут опаснее в обе стороны: «везём всё» однажды
# потащит на сервер чужой файл, «не везём ничего» оставит его без нужного.
RELEASE_ROOT_FILES = (
    # Реестр копий всех программ сервера. Везём, потому что на него ССЫЛАЕТСЯ
    # CLAUDE.md, а его мы везём: ссылка на файл, которого на сервере нет, —
    # то же обещание, которого интерфейс не выполнит. И читают его с консоли
    # сервера ровно тогда, когда уже беда.
    "BACKUPS.md",
    "CLAUDE.md",            # правила проекта; его и так везёт почти каждый патч
    "alembic.ini",          # без него не пройдут миграции
    "create_admin_user.py",
    "requirements.txt",
    "reset_password.py",
)

# Намеренно НЕ везём, и причина у каждого своя. `1c/` — целый каталог: его
# копия на диске сервера на работу не влияет, а разошедшийся sha развалил бы
# накат целиком (об этом отдельно в CLAUDE.md).
NOT_SHIPPED_DIRS = ("1c/",)
NOT_SHIPPED_ROOT = (
    ".env.example",   # образец; настоящий .env на сервере свой и в git не лежит
    ".gitignore",     # про репозиторий, на установку не влияет
    "CHANGELOG_обновление.md",
    "HANDOFF.md",
    "README.md",
    "АРХИТЕКТУРА.md",
    "АУДИТ.md",
    "АУДИТ_2026-09-19.md",
    "АУДИТ_2026-09-21.md",
    "АУДИТ_2026-09-21_полный.md",
    "АУДИТ_2026-09-23.md",
    "ДОКУМЕНТАЦИЯ.md",
    "ПЕРЕДАЧА.md",
    "ПЕРЕДАЧА_В_ВЕБ.md",
    "СТАТУС_ГОТОВНОСТИ.md",
)


def tracked_files(root: str) -> list[str]:
    """Все файлы репозитория — спрашиваем git, а не обход диска.

    Обход подобрал бы то, чего в релизе нет и быть не должно: временную копию
    модуля, которую заводит тест (`scripts/_probe_invariant_broken.py`),
    локальную базу, мусор. Опись — это утверждение о РЕПОЗИТОРИИ, и его знает
    git; соберись она с диска, она однажды потребовала бы от сервера файл,
    который жил только у меня.
    """
    import subprocess

    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                             check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        # Опись — ДИАГНОСТИКА, а патч — то, ради чего всё затевалось: нет git
        # или корень не репозиторий (так собирают патч в проверках, с
        # подменённым корнем) — отдаём пустую опись, но патч собираем. Обратный
        # порядок означал бы, что новая проверка отняла возможность привезти
        # файлы на боевой сервер.
        return []
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def in_release(rel: str) -> bool:
    if rel.startswith(NOT_SHIPPED_DIRS) or rel in NOT_SHIPPED_ROOT:
        return False
    return rel.startswith(RELEASE_DIRS) or rel in RELEASE_ROOT_FILES


def inventory_of(root: str) -> list[tuple[str, str]]:
    """Опись релиза: путь и sha256 КАЖДОГО файла, который обязан лежать на
    сервере, — независимо от того, везёт ли его этот патч.

    В этом вся суть. Сверка sha в патче отвечает «доехало ли то, что я послал»,
    а на вопрос «а всё ли вообще на месте» не отвечает по построению: она
    сверяет то, что В патче, а не то, чего в нём нет. 15.10 так и вышло —
    `scripts/probe_accounts.py` не привозил ни один патч, и десять тестов
    упали на бою через тридцать три минуты после начала наката.
    """
    found = []
    for rel in tracked_files(root):
        if not in_release(rel):
            continue
        with open(os.path.join(root, rel), "rb") as f:
            found.append((rel, hashlib.sha256(f.read()).hexdigest()))
    return sorted(found)


APPLY_TEMPLATE = r'''import base64, datetime, hashlib, io, os, shutil
ROOT = r"C:\sync_admin"
TAG = {tag!r}
FILES = {files!r}
DELETE = {delete!r}
INVENTORY = {inventory!r}
RELEASE_DIRS = {release_dirs!r}

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

# ---- ОПИСЬ: диск сервера — это репозиторий? ----
#
# Сверка sha у файлов патча отвечает на вопрос «доехало ли то, что я послал», и
# на вопрос «а всё ли на месте» не отвечает ПО ПОСТРОЕНИЮ: она сверяет то, что В
# патче, а не то, чего в нём нет. 15.10 это остановило накат живого сервера —
# `scripts/probe_accounts.py` не привозил ни один патч, его там не было вовсе, и
# узналось это из десяти упавших тестов через тридцать три минуты после начала.
# Опись несёт sha КАЖДОГО файла релиза, поэтому отвечает на все три вопроса
# сразу: чего нет, что разошлось и что лежит лишнего (осадок прежних патчей —
# они ведь только пишут и никогда не удаляют).
#
# Накат опись НЕ останавливает, и это сознательно: первый же прогон на живом
# сервере покажет настоящий масштаб, а проверка, которая в первый день
# отказывает исправной системе, кончается тем, что её отключают. Станет отказом
# тогда, когда будет известно, на чём именно отказывать.
try:
    if not INVENTORY:
        # Пустая опись — не «на диске лишнее всё»: без списка известных файлов
        # в «лишние» ушёл бы ВЕСЬ релиз, то есть триста с лишним имён тревоги
        # на исправной установке. Отсутствие ответа называем отсутствием.
        raise RuntimeError("опись в патч не попала — сверять не с чем")
    missing = []
    differs = []
    for rel, expect in INVENTORY:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(path):
            missing.append(rel)
            continue
        with io.open(path, "rb") as f:
            if sha(f.read()) != expect:
                differs.append(rel)

    known = set(rel for rel, _h in INVENTORY)
    # Сами следы наката: их пишет этот же скрипт, в релизе их нет и быть не
    # должно, а в «лишних» они торчали бы каждый раз.
    known.add("deploy/INSTALLED_TAG")
    known.add("deploy/INVENTORY.txt")
    extra = []
    for d in RELEASE_DIRS:
        top = os.path.join(ROOT, d.replace("/", os.sep))
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                rel = rel.replace(os.sep, "/")
                if rel in known or name.endswith(".pyc") or ".bak_" in name:
                    continue
                extra.append(rel)
    extra.sort()

    lines = ["опись " + TAG + " " + stamp + " UTC",
             "ожидается в релизе: " + str(len(INVENTORY)),
             "нет на диске: " + str(len(missing)),
             "разошлись с релизом: " + str(len(differs)),
             "лишние, в релизе их нет: " + str(len(extra))]
    for title, items in (("НЕТ НА ДИСКЕ", missing),
                         ("РАЗОШЛИСЬ С РЕЛИЗОМ", differs),
                         ("ЛИШНИЕ", extra)):
        if items:
            lines.append("")
            lines.append(title + " (" + str(len(items)) + "):")
            for rel in items:
                lines.append("    " + rel)
    inv_path = os.path.join(ROOT, "deploy", "INVENTORY.txt")
    # utf-8-SIG: опись читает ЧЕЛОВЕК, и читает он её блокнотом на Windows
    # Server 2016, а тот без BOM считает файл системной кодировкой — вместо
    # имён получится мозаика ровно там, где по ним принимают решение.
    with io.open(inv_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")

    print("опись: ожидается", len(INVENTORY), "| нет на диске", len(missing),
          "| разошлись", len(differs), "| лишних", len(extra))
    for title, items in (("нет на диске", missing),
                         ("разошёлся", differs),
                         ("лишний", extra)):
        for rel in items[:10]:
            print("    " + title + ":", rel)
        if len(items) > 10:
            print("    ... и ещё", len(items) - 10,
                  "- весь список в deploy/INVENTORY.txt")
except Exception as exc:
    # Опись — диагностика, и ронять ею накат нельзя: файлы уже записаны и
    # сверены по sha, а отказ ПОСЛЕ записи человек прочитает как «патч не
    # встал» и пойдёт откатывать то, что встало верно. Но и глотать нельзя:
    # причина печатается, иначе молчащая опись неотличима от сошедшейся.
    print("опись не собралась:", exc)

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


def subjects_of(root: str, files: list[str]) -> list[str]:
    """Файлы репозитория, которые ЧИТАЮТ или ЗАПУСКАЮТ тесты из этого патча.

    Патч собирают по `git diff`, то есть по тому, что изменено, — а тесту нужен
    ещё и его ПРЕДМЕТ. 15.10 на pm162 это остановило накат живого сервера:
    `tests/test_probe_accounts.py` правился и уехал, а
    `scripts/probe_accounts.py` не менялся с 05.10 — и на сервере его не было
    ВОВСЕ. Патчи только пишут и никогда не удаляют, значит файл, который ни один
    патч не привозил, там просто отсутствует, и никакая сверка sha об этом не
    скажет: сверяется то, что в патче, а не то, чего в нём нет. Десять тестов
    упали с «can't open file», причём узнать об этом можно было только после
    тридцати трёх минут pytest на бою, уже после копии базы и миграций.

    Предмет ищется по ЛИТЕРАЛАМ: тест называет свой скрипт строкой
    (`ROOT / "scripts" / "probe_accounts.py"`), и ровно этот случай гарантированно
    падает, если файла нет. Тест, который берёт каталог ГЛОБОМ, сюда не попадает
    намеренно: неполный каталог он не роняет, он просто проверяет меньше — беда
    другого рода и другого размера.

    Дотягиваем, а не отказываем: отказ пришлось бы обходить руками на каждом
    патче, а дотянутый файл при совпадении sha на сервере даже не пишется. Но
    молча это делать нельзя — список печатается, иначе патч однажды увезёт не то,
    что человек собирался.
    """
    import ast

    known = {f.replace("\\", "/") for f in files}
    found: set[str] = set()
    # Транзитивно: тест может читать ДРУГОЙ тест (так делает проверка самого
    # генератора), а у того свой предмет. Один уровень оставил бы ровно ту же
    # дыру на шаг глубже, а шагов тут считанные единицы — множество файлов
    # конечно, и каждый проход либо что-то добавляет, либо заканчивает обход.
    queue = [f.replace("\\", "/") for f in files]
    while queue:
        rel = queue.pop()
        if not rel.startswith("tests/") or not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            name = node.value.replace("\\", "/")
            # Наружу корня не выходим: литерал в тесте — чужая строка, а патч
            # пишет на боевой сервер по этим же путям. `os.path.join` с
            # абсолютным путём корень ПОДМЕНЯЕТ, то есть один такой литерал
            # увёз бы на сервер файл откуда угодно с диска.
            if (not name.endswith(".py") or os.path.isabs(name)
                    or name.startswith("/") or ".." in name.split("/")):
                continue
            for candidate in (name, "scripts/" + name):
                if candidate in known or candidate in found:
                    continue
                if os.path.isfile(os.path.join(root, candidate)):
                    found.add(candidate)
                    queue.append(candidate)
                    break
    return sorted(found)


def build(root: str, files: list[str], tag: str, parts: int, width: int,
          delete: list[str] | None = None) -> tuple[str, dict]:
    pulled = subjects_of(root, files)
    files = list(files) + pulled
    entries = []
    for rel in files:
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
        entries.append((rel, hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode("ascii")))
    inventory = inventory_of(root)
    apply_src = APPLY_TEMPLATE.format(
        tag=tag, files=entries, delete=list(delete or []),
        inventory=inventory, release_dirs=RELEASE_DIRS).encode("utf-8")
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
            "parts": len(pieces), "full_sha": full_sha, "pulled_in": pulled,
            "inventory": len(inventory)}
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
    for rel in info["pulled_in"]:
        print(f"+ дотянут предмет теста: {rel}")
    if not info["inventory"]:
        # Молча патч без описи выпускать нельзя: на сервере она отвечает на
        # вопрос «а всё ли на месте», и её отсутствие — это минус один ответ,
        # а не «всё хорошо».
        print("! ОПИСЬ НЕ СОБРАНА (корень не репозиторий или нет git) — "
              "патч не ответит, всё ли на сервере на месте")
    print(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
