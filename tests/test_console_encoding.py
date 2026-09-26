"""Вывод скриптов обязан пережить консоль боевого сервера.

26.09 накат встал на красных тестах: `scripts/catalog_diff.py` и
`scripts/probe_zero_sends.py` падали с `UnicodeEncodeError` на стрелке `→`
(U+2192). В cp1251 её нет, а Python на сервере пишет stdout именно в ней —
то есть скрипты не работали ТАМ ВООБЩЕ, падая на первой же строке вывода.
Здесь, на Linux с UTF-8, оба были зелёными: ровно тот класс, о котором
`CLAUDE.md` предупреждает первым абзацем.

Тем же сканером нашлись ещё трое, которых просто не успели запустить:
`probe_offset.py` (`≈`, `−`), `restore_offsets_from_backup.py` и
`e2e_audit.py` (`→`). Последний особенно дорог: скрипт восстановления
порогов запускают ИМЕННО тогда, когда уже беда, и падение вместо плана
восстановления там стоит дороже всего.

Проверок две, и они про разное.

**Сканер** смотрит на НАШИ строки: они обязаны кодироваться в cp1251. Он
называет файл и строку, то есть чинится по его выводу за минуту.

**Прогон в cp1251** (`PYTHONIOENCODING`) — про ЧУЖИЕ данные: тело ответа
площадки из `last_error`, названия товаров, артикулы. Держать их в пределах
кодировки мы не можем и не будем; вместо этого скрипты глушат ошибку
кодирования (`sys.stdout.reconfigure(errors="replace")`), и «?» вместо
символа несравнимо лучше, чем отсутствие ответа. Сканер этого не увидел бы
никогда: в исходнике таких символов нет, они приезжают из базы.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "scripts").glob("*.py"))

# Кодировка консоли боевого сервера. Не UTF-8 и не cp866: `UnicodeEncodeError`
# 26.09 пришёл именно от cp1251 — Python берёт её как локальную кодировку
# Windows с русской локалью, когда вывод перенаправлен.
CONSOLE = "cp1251"


def _printed_literals(path: Path):
    """Строковые куски, которые уходят в `print` — включая части f-строк."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield sub.lineno, sub.value


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_printed_text_survives_the_server_console(path):
    """Символ, которого нет в cp1251, роняет ВЕСЬ вывод, а не портит строку.

    Цена не в опрятности: человек запускает скрипт, чтобы получить ответ, и
    получает трассировку. А на машине разработки всё зелено, потому что тут
    UTF-8.
    """
    offenders = []
    for lineno, text in _printed_literals(path):
        bad = sorted({ch for ch in text
                      if ch.encode(CONSOLE, "replace") == b"?" and ch != "?"})
        if bad:
            offenders.append(f"строка {lineno}: {bad} в {text[:60]!r}")
    assert offenders == [], (
        f"{path.name} печатает символы, которых нет в {CONSOLE} — на сервере "
        f"это UnicodeEncodeError вместо ответа:\n  " + "\n  ".join(offenders)
        + "\n  Замените на ASCII: «->» вместо стрелки, «~» вместо «≈».")


def test_the_scanner_would_actually_catch_the_arrow():
    """Проверка на саму проверку: сканер, который ничего не видит, молчит так
    же, как сканер, которому нечего сказать.

    Стрелка — тот самый символ, на котором встал накат 26.09.
    """
    assert "→".encode(CONSOLE, "replace") == b"?", (
        "стрелка внезапно кодируется — сканер выше перестал что-либо значить")
    assert "«Товары»".encode(CONSOLE, "replace") != b"?" * 8, (
        "кириллица и кавычки в cp1251 ЕСТЬ: запрещать их значило бы "
        "переписать весь вывод без причины")


@pytest.mark.parametrize("name", ["probe_zero_sends.py", "catalog_diff.py",
                                  "probe_offset.py"])
def test_scripts_that_print_foreign_data_do_not_die_on_it(name):
    """Свои строки мы держим в кодировке, чужие — не можем.

    `last_error` несёт ТЕЛО ОТВЕТА ПЛОЩАДКИ, а названия и артикулы приезжают
    из 1С и из каталога кабинета. Один неожиданный символ оттуда — и вывод
    обрывается на середине, унося и то, что уже посчитано.
    """
    src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    assert 'reconfigure(errors="replace")' in src, (
        f"{name} печатает чужие данные без защиты вывода")
