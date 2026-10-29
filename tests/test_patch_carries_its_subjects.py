"""Патч обязан увозить ПРЕДМЕТ теста, а не только сам тест.

15.10 на pm162 это остановило накат живого сервера. Список файлов патча
собирается по `git diff`, то есть по тому, что изменено: `test_probe_accounts.py`
правился и уехал, а `scripts/probe_accounts.py` не менялся с 05.10 — и на сервере
его не было ВОВСЕ. Патчи только пишут и никогда не удаляют, поэтому файл, который
ни один патч не привозил, там просто отсутствует, а сверка sha об этом молчит:
она сверяет то, что в патче, а не то, чего в нём нет.

Десять тестов упали с «can't open file», причём увидеть это можно было только
после ТРИДЦАТИ ТРЁХ минут pytest на боевом сервере — уже после копии базы и
миграций, то есть ровно в том состоянии, которое накат существует чтобы пройти
насквозь.

Проверяем по СЛЕДСТВИЮ: собираем патч тем же скриптом, которым собирают боевой,
распаковываем полезную нагрузку и смотрим, какие файлы ЛЯГУТ НА СЕРВЕР. Проверять
печать генератора мало — предмет правки это то, что окажется на диске.
"""
import ast
import base64
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = ROOT / "scripts" / "gen_whole_file_patch.py"


def _build(tmp_path, *files, root=ROOT) -> tuple[set[str], str]:
    """Собрать патч и вернуть (пути, которые он увозит; что он напечатал)."""
    out = tmp_path / "probe_deploy.ps1"
    env = dict(os.environ)
    # Кодировка задаётся с ОБЕИХ сторон и одинаковая — см.
    # tests/test_subprocess_encoding.py.
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, str(GEN), "--tag", "probe", "--parts", "1",
         "--root", str(root), "--out", str(out), *files],
        cwd=str(ROOT), capture_output=True, encoding="utf-8",
        errors="replace", env=env)
    assert done.returncode == 0, done.stdout + done.stderr

    text = out.read_text(encoding="utf-8-sig")
    blocks = re.findall(r"@'\n(.*?)\n'@", text, re.S)
    b64 = "".join("".join(b.split()) for b in blocks)
    apply_src = zlib.decompress(base64.b64decode(b64)).decode("utf-8")
    listing = re.search(r"^FILES = (\[.*?\])\nDELETE", apply_src, re.S | re.M)
    assert listing, "в apply-скрипте не нашёлся список файлов"
    return {rel for rel, _sha, _data in ast.literal_eval(listing.group(1))}, done.stdout


def test_the_script_a_test_runs_goes_with_it(tmp_path):
    """Главное: тест назвал свой скрипт строкой — скрипт уезжает вместе с ним."""
    carried, printed = _build(tmp_path, "tests/test_probe_accounts.py")

    assert "scripts/probe_accounts.py" in carried, (
        "тест уехал без предмета — на сервере он упадёт с «can't open file», "
        "и узнается это только в накате, после копии базы")
    assert "scripts/probe_accounts.py" in printed, (
        "дотянутый файл обязан быть НАЗВАН: молча увезённый файл однажды "
        "окажется не тем, что человек собирался поставить")


def test_it_pulls_the_subject_but_not_the_whole_repository(tmp_path):
    """Обратная проверка, без которой первая молчала бы и на «везём всё».

    Сканер кодировки берёт каталоги ГЛОБОМ и ни одного скрипта по имени не
    называет: дотягивать ему нечего. Пройди эта проверка с «везём всё подряд», и
    патч раздувался бы до репозитория, а сверка sha перестала бы что-либо значить.
    """
    carried, _ = _build(tmp_path, "tests/test_subprocess_encoding.py")

    assert carried == {"tests/test_subprocess_encoding.py"}, carried


def test_a_patch_without_tests_is_left_alone(tmp_path):
    """Патч из одних боевых файлов не трогаем вовсе: предмета искать не у кого."""
    carried, printed = _build(tmp_path, "app/backup.py")

    assert carried == {"app/backup.py"}
    assert "дотянут" not in printed


def test_the_subject_of_a_subject_goes_too(tmp_path):
    """Обход ТРАНЗИТИВНЫЙ: тест читает другой тест, а у того свой предмет.

    Ровно этот случай — сам этот файл: он собирает патч по
    `tests/test_probe_accounts.py`, значит тот обязан лежать на диске, а вместе с
    ним и `scripts/probe_accounts.py`. Один уровень оставил бы ту же дыру на шаг
    глубже.

    Корень здесь СВОЙ, собранный в tmp_path, и это не опрятность. Первая
    редакция проверяла транзитивность на настоящем репозитории и была зелёной по
    НЕВЕРНОЙ причине: имя `scripts/probe_accounts.py` стоит литералом в этом же
    файле, то есть дотягивалось напрямую, и мутация «обход на один уровень»
    проверку не роняла. На синтетическом корне путь к цели только один — через
    второй тест.
    """
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests" / "test_outer.py").write_text(
        'INNER = "tests/test_inner.py"\n', encoding="utf-8")
    (root / "tests" / "test_inner.py").write_text(
        'SUBJECT = "deep_probe.py"\n'
        # Литерал в тесте — чужая строка, а патч пишет по этим путям на боевой
        # сервер: выход за корень обязан быть отброшен, иначе один такой литерал
        # увезёт туда файл откуда угодно с диска.
        'OUTSIDE = "../outside.py"\n'
        'ABSOLUTE = "/etc/passwd.py"\n', encoding="utf-8")
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    (root / "scripts" / "deep_probe.py").write_text("x = 1\n", encoding="utf-8")

    carried, _ = _build(tmp_path, "tests/test_outer.py", root=root)

    assert "tests/test_inner.py" in carried, carried
    assert "scripts/deep_probe.py" in carried, (
        "предмет предмета не дотянулся — обход не транзитивен")
    assert not [c for c in carried if ".." in c or c.startswith("/")], carried


def test_a_helper_reached_only_by_import_goes_too(tmp_path):
    """Предмет бывает назван ИМПОРТОМ, а не строкой — и это живой случай.

    `tests/test_probe_pool.py` запускает `scripts/probe_pool.py` (его имя там
    литералом, он дотягивается), а сам `probe_pool.py` ИМПОРТИРУЕТ
    `probe_movements` — поиск строк один на все probe-скрипты, намеренно. Этот
    второй файл не называет никто, его приходилось дописывать в патч руками, а
    забудь человек — на сервере скрипт упал бы `ModuleNotFoundError` в тот
    день, когда его открывают, то есть когда уже беда.

    Корень СВОЙ: на настоящем репозитории `probe_movements` дотягивался бы и по
    другим путям, и мутация «импорты не смотрим» проверку не уронила бы.
    """
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests" / "test_tool.py").write_text(
        'SCRIPT = "scripts/tool.py"\n', encoding="utf-8")
    (root / "scripts" / "tool.py").write_text(
        "import helper\nfrom deep_helper import thing\n", encoding="utf-8")
    (root / "scripts" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (root / "scripts" / "deep_helper.py").write_text(
        "import third\n", encoding="utf-8")
    (root / "scripts" / "third.py").write_text("x = 1\n", encoding="utf-8")

    carried, printed = _build(tmp_path, "tests/test_tool.py", root=root)

    assert "scripts/helper.py" in carried, carried
    assert "scripts/deep_helper.py" in carried, carried
    # Транзитивно и через скрипты: helper импортирует своего helper'а.
    assert "scripts/third.py" in carried, carried
    assert "scripts/helper.py" in printed, "дотянутый файл обязан быть НАЗВАН"


def test_a_test_helper_module_goes_too(tmp_path):
    """`from tests.factories import …` — тот же случай с другой стороны.

    Помощник тестов обычной работой не возится: его правят раз в полгода, а
    тесты, которые его импортируют, правят постоянно. На сервере его может не
    быть вовсе, и тогда падает не один тест, а каждый, кто его зовёт.
    """
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_uses.py").write_text(
        "from tests.helpers import make\nimport sidecar\n", encoding="utf-8")
    (root / "tests" / "helpers.py").write_text("def make(): pass\n", encoding="utf-8")
    (root / "tests" / "sidecar.py").write_text("x = 1\n", encoding="utf-8")

    carried, _ = _build(tmp_path, "tests/test_uses.py", root=root)

    assert "tests/helpers.py" in carried, carried
    # Голое имя ищется и в tests/, а не только в scripts/.
    assert "tests/sidecar.py" in carried, carried


def test_the_import_scan_does_not_drag_in_the_world(tmp_path):
    """Обратная проверка, и она тут важнее первой.

    Стандартная библиотека, чужие пакеты и боевой код дотягиваться НЕ должны.
    Про `app/*` это решение, а не упущение: боевой код едет в патчах
    постоянно — модуль, которого на сервере нет, это новый модуль, а новый
    модуль по построению попадает в `git diff`. Зато тест, импортирующий
    `app.models`, тянул бы сотни килобайт в КАЖДЫЙ патч, который его правит, а
    нагрузка и так под мегабайт. Про боевой файл, однажды не доехавший,
    отвечает ОПИСЬ — это её работа.
    """
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "app" / "workers").mkdir(parents=True)
    (root / "tests" / "test_plain.py").write_text(
        "import os\nimport sys\nimport pytest\n"
        "from app.workers.order_audit import audit_all\n"
        "from sqlalchemy.orm import Session\n", encoding="utf-8")
    (root / "app" / "workers" / "order_audit.py").write_text("x = 1\n",
                                                             encoding="utf-8")

    carried, printed = _build(tmp_path, "tests/test_plain.py", root=root)

    assert carried == {"tests/test_plain.py"}, carried
    assert "дотянут" not in printed, printed


def test_a_default_path_inside_a_script_is_not_followed(tmp_path):
    """Литералы смотрим ТОЛЬКО в тестах, и это не мелочь.

    У скрипта строка с путём — это его УМОЛЧАНИЕ, и у `check_1c_module.py` оно
    ведёт в `1c/`, который накат не везёт намеренно: его копия на диске сервера
    на работу не влияет, а разошедшийся sha развалил бы накат целиком. Пойди
    обход по литералам внутри скриптов, патч увёз бы туда ровно то, чего там
    быть не должно.
    """
    root = tmp_path / "root"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "1c").mkdir()
    (root / "tests" / "test_tool.py").write_text(
        'SCRIPT = "scripts/tool.py"\n', encoding="utf-8")
    (root / "scripts" / "tool.py").write_text(
        'LIVE = "1c/module.py"\n', encoding="utf-8")
    (root / "1c" / "module.py").write_text("x = 1\n", encoding="utf-8")

    carried, _ = _build(tmp_path, "tests/test_tool.py", root=root)

    assert carried == {"tests/test_tool.py", "scripts/tool.py"}, carried
