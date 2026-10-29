"""Тест, которому нужен РЕПОЗИТОРИЙ, обязан быть помечен `@repository_only`.

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

**29.10 выяснилось, что механизмов ДВА, и правило было применено к одному.**
Опись релиза (`gen_whole_file_patch.inventory_of`) берёт список файлов из
`git ls-files`, а git на боевом сервере не установлен вовсе: `tracked_files`
там отдаёт пустой список НАМЕРЕННО (иначе патч перестал бы собираться), и
тест, который строит «сервер» ПО ОПИСИ, проверяет на сервере пустоту и падает
ВСЕГДА. На pm167 так встали шесть тестов `test_server_inventory.py` — накат
остановился перед перезапуском служб, уже после копии базы и миграций, то есть
ровно тем же способом, что pm147 и pm148. Третий случай подряд на «проверка
была уже, а применена не ко всему», поэтому правило расширено здесь, а не
правкой по месту: сканер требует маркер и у тех, кто добирается до описи.
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


def _runs_a_script_without_a_path(func: ast.FunctionDef) -> bool:
    """Функция запускает наш скрипт, НЕ давая ему пути.

    Тогда он берёт УМОЛЧАНИЕ, а умолчание у `check_1c_module.py` — путь внутрь
    `1c/`. На сервере этого файла может не быть вовсе, и скрипт ответит «Файла
    нет». 02.10 ровно так встал pm148, уже после починки пятнадцати соседей:
    смотреть надо не на то, НАЗЫВАЕТ ли тест файл, а на то, доберётся ли до
    него запуск.

    Признак — список из двух элементов в `subprocess.run([...])`: интерпретатор
    и сам скрипт, аргументов нет.
    """
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run" and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.List) and len(first.elts) == 2:
            return True
    return False


# Помощники генератора патча, которые спрашивают git. Признак — ИМЯ, а не
# модуль-алиас: `gen.inventory_of`, `inventory_of`, `patch.tracked_files` —
# всё это один и тот же вопрос «что в репозитории», и на сервере ответ на него
# пуст. Перечислены поимённо: требовать маркер по слову «git» где угодно
# значило бы ругаться на исправное, а проверка, ругающаяся на исправное,
# кончается тем, что её отключают.
GIT_BACKED_HELPERS = {"tracked_files", "inventory_of"}


def _asks_git_what_is_in_the_repository(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr in GIT_BACKED_HELPERS:
            return True
        if isinstance(node, ast.Name) and node.id in GIT_BACKED_HELPERS:
            return True
    return False


def _functions_touching_1c(tree: ast.AST) -> set[str]:
    """Функции файла, которые доберутся до `1c/` — сами или через помощника."""
    marked = _paths_into_1c(tree)
    if not marked:
        return set()

    funcs = {n.name: _referenced_names(n) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}

    touching = {name for name, used in funcs.items() if used & marked}
    touching |= {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and _runs_a_script_without_a_path(n)}
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


def _functions_needing_the_repository(tree: ast.AST) -> set[str]:
    """Функции файла, которым нужен репозиторий: каталог `1c/` ЛИБО опись.

    Замыкание считается по ОБЪЕДИНЕНИЮ, а не по каждому механизму отдельно:
    тест может звать помощника, который читает опись, ровно так же, как
    помощника, который читает модуль 1С, — и разбей мы это на два прохода,
    смешанная цепочка (`тест -> помощник описи -> ...`) провалилась бы между
    ними.
    """
    funcs = {n.name: _referenced_names(n) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}

    needing = _functions_touching_1c(tree)
    needing |= {n.name for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and _asks_git_what_is_in_the_repository(n)}

    changed = True
    while changed:
        changed = False
        for name, used in funcs.items():
            if name not in needing and used & needing:
                needing.add(name)
                changed = True
    return needing


def _unmarked_tests_reading_1c() -> list[str]:
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        touching = _functions_needing_the_repository(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("test_")
                    and node.name in touching
                    and not _is_marked(node)):
                found.append(f"{path.name}::{node.name}")
    return found


def test_every_test_that_needs_the_repository_is_repository_only():
    unmarked = _unmarked_tests_reading_1c()

    assert unmarked == [], (
        "эти тесты читают 1c/ или опись релиза без @repository_only и на боевом "
        "сервере упадут ВСЕГДА, остановив накат перед перезапуском служб: "
        + ", ".join(unmarked))


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
    # Запуск БЕЗ пути берёт умолчание, то есть тот же `1c/` — pm148.
    assert "test_without_an_argument_it_says_this_is_not_the_live_epf" in touching
    # А эти дают путь явно и на сервере как раз работают — гасить их нельзя.
    assert "test_a_missing_file_is_refused_plainly" not in touching
    assert "test_the_epf_itself_is_refused_with_the_way_out" not in touching

    assert _functions_touching_1c(
        ast.parse((TESTS / "test_1c_protocol.py").read_text(encoding="utf-8"))
    ) == set(), "файл говорит про 1c только в докстроке — каталога он не читает"


def test_the_scanner_sees_the_second_mechanism_too():
    """Опись: сканер обязан РАЗЛИЧАТЬ и здесь, иначе он сузился до пустоты.

    В `test_server_inventory.py` живут оба сорта: одни добираются до описи
    (сами или через `_server`, который строит «сервер» по ней) — на сервере
    они падают всегда; другие читают ИСХОДНИК `update_windows.ps1`, и там они
    как раз работают. Пометь мы файл целиком — погасили бы вторых, а это
    единственная проверка, что у описи есть читатель и что он под `try`.
    """
    tree = ast.parse((TESTS / "test_server_inventory.py").read_text(encoding="utf-8"))
    needing = _functions_needing_the_repository(tree)

    # Зовёт `gen.inventory_of` напрямую.
    assert "test_the_inventory_covers_the_tools_and_skips_what_we_never_ship" in needing
    # Зовёт `gen.tracked_files` напрямую.
    assert "test_every_file_in_the_root_is_a_decision" in needing
    # А эти — транзитивно, через `_server`: своего обращения к описи у них нет.
    assert "_server" in needing
    assert "test_a_disk_that_matches_the_release_says_so" in needing
    assert "test_the_inventory_names_what_is_missing_changed_and_extra" in needing

    # Эти два читают исходник скрипта наката — репозиторий им не нужен.
    assert "test_the_inventory_has_a_reader_in_the_update_script" not in needing
    assert "test_reading_the_inventory_can_never_abort_the_update" not in needing
