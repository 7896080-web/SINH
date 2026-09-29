"""Модуль обработки 1С (`1c/ОбменССайтом_МодульОбъекта.txt`) держит три правила,
на которых стоит связь двух программ (ТЗ, разд. 9). Компилятора 1С здесь нет,
поэтому правила проверяются по тексту модуля.

Модуль лежит в каталоге sync_admin (`1c/`), на сервере маркировки его рядом нет,
— проверка только в репозитории.
"""
import re
from pathlib import Path

import pytest

from markapp import onec

MODULE = Path(__file__).resolve().parents[2] / "1c" / "ОбменССайтом_МодульОбъекта.txt"
pytestmark = pytest.mark.skipif(not MODULE.exists(), reason="модуль 1С лежит в репозитории, не на сервере")


def _text():
    return MODULE.read_text(encoding="utf-8-sig")


def _function(name):
    text = _text()
    start = text.index(f"Функция {name}(")
    return text[start:text.index("КонецФункции", start)]


def test_every_command_of_the_program_has_a_branch():
    text = _text()
    for cmd in onec.COMMANDS:
        assert f'Команда = "{cmd}"' in text, cmd


def test_answers_for_marking_go_to_their_own_folder():
    f = _function("КаталогОтветов")
    assert 'СтрНачинаетсяС(Метка, "mark_")' in f and '"marking"' in f
    text = _text()
    assert 'ОпубликоватьФайл(КаталогДляОтветов, "result_"' in text
    assert 'ОпубликоватьФайл(Пар.КаталогResults, "result_"' not in text


def test_supplycheck_is_published_before_the_answer():
    text = _text()
    assert text.index('"supplycheck_" + Метка') < text.index('"result_" + Метка')


def test_movement_is_dated_now_and_not_marked_sync():
    f = _function("ПереместитьПоставку")
    assert "Документ.Дата = ТекущаяДата();" in f
    assert "Дата(Число(" not in f
    assert 'Документ.Комментарий = "mark order_id="' in f
    assert '"sync' not in f


def test_movement_checks_idempotency_before_stock():
    f = _function("ПереместитьПоставку")
    assert f.index("НайтиПроведённоеПеремещение") < f.index("Разбор.Ошибки.Количество()")
    assert f.index("Разбор.Ошибки.Количество()") < f.index("СоздатьДокумент()")


def test_field_count_matches_the_program():
    """Программа шлёт 6 полей; обработка проверяет их число до обращения по индексу."""
    text = _text()
    branch = text[text.index('Команда = "SUPPLY_CHECK" ИЛИ Команда = "SUPPLY_MOVEMENT"'):]
    branch = branch[:branch.index("ИначеЕсли") if "ИначеЕсли" in branch else len(branch)]
    assert "Если Поля.Количество() < 6 Тогда" in branch
    assert branch.index("Поля.Количество() < 6") < branch.index("Поля[1]")

    class _Row:
        def __init__(self, ean, qty):
            self.ean, self.qty = ean, qty

    class _Supply:
        number = "12560"
        supply_date = None
        rows = [_Row("2000000000001", 2)]
    assert len(onec.supply_line("SUPPLY_CHECK", "x", _Supply()).split("|")) == 6


def test_check_file_format_matches_the_program():
    f = _function("СтрокаПроверкиПоставки")
    ret = f[f.index("Возврат ОчиститьПоле(Поз.Баркод)"):]
    assert ret.count('"|"') == len(onec.CHECK_FIELDS) - 1


def test_module_version_is_reported():
    assert re.search(r'Возврат "mark-\d+";', _function("ВерсияМодуля"))
