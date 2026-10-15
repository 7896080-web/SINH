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
