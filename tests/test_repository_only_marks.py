"""Тест, читающий `1c/`, обязан быть помечен `@repository_only`.

Это уже случилось ДВАЖДЫ, и оба раза одинаково: блоки наката каталог `1c/`
намеренно не везут (его копия на диске сервера на работу не влияет, а
разошедшийся sha развалил бы накат целиком), значит там лежит модуль с ПЕРВОЙ
установки. Тест, который вынимает из него возможность и требует, чтобы
проверка закричала, на сервере падает ВСЕГДА — и падает ПЕРЕД перезапуском
служб: накат встаёт на живом сервере, уже после копии базы и миграций, а
починить по месту нельзя — здесь-то он зелёный.

02.10 так встал pm147: шестнадцать падений в `test_check_1c_module.py`.

Поэтому правило закреплено машиной, а не памятью автора.

Сканер смотрит на ПОСТРОЕНИЕ ПУТИ (`ROOT / "1c" / ...`), а не на строку «1c»
где угодно: первая его редакция цепляла `uid_1c` в чужих тестах и «1c/README.md»
в докстроке — то есть требовала маркера там, где каталога нет вовсе. Проверка,
ругающаяся на исправное, кончается тем, что её отключают, а значит перестаёт
ловить и настоящее.

И смотрит он на ФУНКЦИИ, а не на файл целиком: в `test_check_1c_module.py`
половина тестов про отказы скрипта (нет файла, подсунули саму `.epf`) и к
каталогу не обращается вовсе — помечать их значило бы пропускать на сервере
проверки, которые там как раз работают. Обращение считается транзитивно:
тест зовёт помощника, помощник читает `LIVE`.
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MARK = "repository_only"


def _paths_into_1c(tree: ast.AST) -> set[str]:
    """Имена уровня модуля, которым присвоен путь внутрь `1c/`."""
    names = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        builds_path = any(
            isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Div)
            and isinstance(sub.right, ast.Constant) and sub.right.value == "1c"
            for sub in ast.walk(value))
        if not builds_path:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _referenced_names(func: ast.FunctionDef) -> set[str]:
    return {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}


def _functions_touching_1c(tree: ast.AST) -> set[str]:
    """Функции файла, которые доберутся до `1c/` — сами или через помощника."""
    marked = _paths_into_1c(tree)
    if not marked:
        return set()

    funcs = {n.name: _referenced_names(n) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}

    touching = {name for name, used in funcs.items() if used & marked}
    # Транзитивное замыкание: тест зовёт помощника, помощник читает путь.
    changed = True
    while changed:
        changed = False
        for name, used in funcs.items():
            if name not in touching and used & touching:
                touching.add(name)
                changed = True
    return touching


def _is_marked(func: ast.FunctionDef) -> bool:
    for dec in func.decorator_list:
        for node in ast.walk(dec):
            if isinstance(node, ast.Name) and node.id == MARK:
                return True
            if isinstance(node, ast.Attribute) and node.attr == MARK:
                return True
    return False


def _unmarked_tests_reading_1c() -> list[str]:
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        touching = _functions_touching_1c(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("test_")
                    and node.name in touching
                    and not _is_marked(node)):
                found.append(f"{path.name}::{node.name}")
    return found


def test_every_test_that_reads_the_1c_directory_is_repository_only():
    unmarked = _unmarked_tests_reading_1c()

    assert unmarked == [], (
        "эти тесты читают 1c/ без @repository_only и на боевом сервере упадут "
        "ВСЕГДА, остановив накат перед перезапуском служб: " + ", ".join(unmarked))


def test_the_scanner_sees_the_tests_it_is_written_for():
    """Сканер, которому нечего смотреть, молчит так же, как исправный.

    Поэтому проверяем не только «пусто», но и что он РАЗЛИЧАЕТ: тест, читающий
    боевой модуль, он видит, а соседний, который про отказ самого скрипта, —
    нет. Без второй половины сканер, сузившийся до пустоты, прошёл бы молча.
    """
    tree = ast.parse((TESTS / "test_check_1c_module.py").read_text(encoding="utf-8"))
    touching = _functions_touching_1c(tree)

    assert "test_the_live_module_passes_everything" in touching
    assert "test_a_lost_capability_is_named" in touching      # через помощника
    assert "test_a_missing_file_is_refused_plainly" not in touching
    assert "test_the_epf_itself_is_refused_with_the_way_out" not in touching

    assert _functions_touching_1c(
        ast.parse((TESTS / "test_1c_protocol.py").read_text(encoding="utf-8"))
    ) == set(), "файл говорит про 1c только в докстроке — каталога он не читает"
