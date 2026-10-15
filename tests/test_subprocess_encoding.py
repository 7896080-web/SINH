"""Тест, который запускает скрипт подпроцессом, обязан ПРИБИТЬ кодировку с ОБЕИХ
сторон — и это сканер, а не отдельная проверка в каждом тесте.

У перенаправленного вывода питон берёт кодировку из локали дочернего процесса:
здесь UTF-8, а на боевом Windows cp1251. Пока ни одна сторона кодировку не
называет, они совпадают случайно — обе смотрят в локаль, — и тест зелен на обеих
машинах. Стоит назвать её ОДНОЙ стороне, и они расходятся:

* назвал читатель (`encoding="utf-8"`), а писателю не сказал — на Windows
  приедет cp1251, и тест упадёт на мозаике из НАШИХ ЖЕ русских строк. Это
  15.10 и случилось в `test_probe_growth.py`: семь падений на боевом сервере,
  накат встал ПЕРЕД перезапуском служб, а здесь всё зелено, то есть починить по
  месту нельзя;
* назвал писателя (оператор выставил `PYTHONIOENCODING` в консоли, разбирая
  логи), а читатель смотрит в локаль — падают тесты при полностью исправном
  скрипте. Это уже проходили на `reset_password.py`, и там же лежит починка,
  которую забыли применить ко всему остальному.

Поэтому правило общее: у вызова обязан быть явный `encoding=`, а в окружении
дочернего процесса — `PYTHONIOENCODING`, И ОДНИМ И ТЕМ ЖЕ выражением. Разойдись
значения, тест упадёт ВЕЗДЕ одинаково (писатель действительно пишет тем, о чём
попросили) — то есть дефект перестаёт быть платформенным.

Третий случай подряд на правиле «проверка была уже, а применена не ко всему» —
отсюда сканер.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = sorted(ROOT.joinpath("tests").glob("*.py")) + \
    sorted(ROOT.joinpath("scripts").glob("*.py"))


def _spawns_python(call: ast.Call) -> bool:
    """Вызов запускает ИМЕННО питон: `[sys.executable, ...]` первым аргументом.

    Фильтр по `sys.executable`, а не по любому `subprocess.run`: кодировку
    вывода из локали берёт питон, а чужая программа (rclone, `sc`) о нашем
    `PYTHONIOENCODING` не знает вовсе, и требовать его от неё значило бы
    ругаться на исправное.
    """
    if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)):
        return False
    for item in call.args[0].elts:
        if (isinstance(item, ast.Attribute) and item.attr == "executable"
                and isinstance(item.value, ast.Name) and item.value.id == "sys"):
            return True
    return False


def _pinned_in_env(tree: ast.AST) -> set[str]:
    """Выражения, которыми в этом файле задают `PYTHONIOENCODING`."""
    pinned = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "PYTHONIOENCODING"):
                pinned.add(ast.unparse(node.value))
    return pinned


def _runs(tree: ast.AST) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run" and _spawns_python(node)):
            found.append(node)
    return found


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_a_spawned_script_is_read_with_the_encoding_it_was_told_to_write(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = _runs(tree)
    if not calls:
        return
    pinned = _pinned_in_env(tree)

    for call in calls:
        where = f"{path.name}:{call.lineno}"
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        assert "encoding" in kwargs, (
            f"{where}: читателю не названа кодировка — на боевом Windows вывод "
            f"приедет в cp1251, и тест упадёт на мозаике из наших же строк")
        read = ast.unparse(kwargs["encoding"])
        assert "env" in kwargs, (
            f"{where}: дочернему процессу не передано окружение, значит "
            f"PYTHONIOENCODING ему не сказать вовсе")
        assert read in pinned, (
            f"{where}: читаем {read}, а PYTHONIOENCODING задан как "
            f"{sorted(pinned) or 'НИКАК'} — писатель и читатель разойдутся")


def test_the_scanner_sees_the_calls_it_is_written_for():
    """Сканер, которому нечего смотреть, молчит так же, как исправный набор.

    Запускают скрипты подпроцессом больше десятка тестов; упади это число до
    единиц, значит фильтр `sys.executable` разошёлся с тем, как вызовы
    написаны, и правило перестало применяться молча."""
    seen = sum(len(_runs(ast.parse(p.read_text(encoding="utf-8"))))
               for p in SCANNED)
    assert seen >= 15, f"сканер видит всего {seen} запусков — проверь фильтр"
