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
    assert f.index("ПроведённаяПоставка(") < f.index("Разбор.Ошибки.Количество()")
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


def test_supply_barcode_lookup_follows_the_sync_admin_dictionary_rule():
    """Товар поставки ищется тем же правилом, что справочник баркодов sync_admin:
    только владелец-номенклатура, и штрихкод на двух товарах — отказ, а не
    `ПЕРВЫЕ 1` наугад. Общая функция (по ней работает sync_admin) не тронута."""
    f = _function("ТоварПоставкиПоШтрихкоду")
    assert "ВЫБРАТЬ РАЗЛИЧНЫЕ ПЕРВЫЕ 2" in f
    assert "Владелец ССЫЛКА Справочник.Номенклатура" in f
    assert "Рез.Неоднозначно = Выборка.Следующий();" in f
    parse = _function("РазобратьПозицииПоставки")
    assert "ТоварПоставкиПоШтрихкоду(Поз.Баркод)" in parse
    assert "НайтиНоменклатуруПоШтрихкоду" not in parse
    assert 'Поз.Статус = "ambiguous"' in parse
    # Позиция без однозначного товара в расчёт остатка не идёт (у неё нет ключа).
    assert 'Если Поз.Статус <> "ok" Тогда' in parse
    assert "ВЫБРАТЬ ПЕРВЫЕ 1" in _function("НайтиНоменклатуруПоШтрихкоду")


def test_repository_module_uses_the_production_exchange_folders():
    """Проверка на копии базы идёт тестовой сборкой с `C:\\sync_test`
    (1c/ОБНОВЛЕНИЕ_ОБРАБОТКИ.md, шаг 4). В репозиторий обязан попадать модуль с
    БОЕВЫМИ путями: собранная из него обработка с `C:\\sync_test` читала бы на бою
    пустую папку, и sync_admin молча перестал бы получать ответы 1С."""
    f = _function("ПараметрыОбмена")
    for d in ("tasks", "results", "archive"):
        assert f'"C:\\sync\\{d}"' in f, d
    assert "sync_test" not in _text()


def test_barcode_dict_goes_to_its_own_folder_before_the_answer():
    """BARCODE_DICT: справочник — файлом barcodes_<метка> в папку ответов задания
    (для mark_ это results\\marking) и РАНЬШЕ ответа: увидев result, программа
    сразу читает справочник. EXPORT_BARCODES sync_admin кладёт файл в общий
    results — его забрало бы приложение синхронизации."""
    text = _text()
    assert 'Команда = "BARCODE_DICT"' in text
    assert 'ОпубликоватьФайл(КаталогДляОтветов, "barcodes_" + Метка' in text
    assert text.index('"barcodes_" + Метка') < text.index('"result_" + Метка')
    assert int(re.search(r'Возврат "mark-(\d+)";', _function("ВерсияМодуля")).group(1)) >= onec.MIN_VERSION["BARCODE_DICT"]


def test_movement_takes_stock_from_the_checked_warehouse():
    """Документ списывает с того склада, по которому сверялся остаток (поле 2)."""
    text = _text()
    assert "ПереместитьПоставку(ИдЗаказа, Поля[2], Поля[3]," in text
    f = _function("ПереместитьПоставку")
    assert "СкладОткуда = НайтиСкладПоКоду(КодСкладаОткуда);" in f
    assert "СкладЦС" not in f


def test_supply_finds_warehouses_by_code_only():
    """Поставка не зовёт НайтиСклад (наименование) — только по коду; общую
    функцию sync_admin не трогаем."""
    text = _text()
    for name in ("РазобратьПозицииПоставки", "ПереместитьПоставку", "ПроведённаяПоставка"):
        assert "НайтиСклад(" not in _function(name), name
    f = _function("НайтиСкладПоКоду")
    assert "Склады.Код = &Код" in f and "ПЕРВЫЕ 2" in f
    assert f.count("ВызватьИсключение") == 3
    assert 'Функция НайтиСклад(Имя)\n\tСклад = Справочники.Склады.НайтиПоНаименованию(Имя, Истина);' in text


def test_supply_idempotency_ignores_reverses():
    f = _function("ПроведённаяПоставка")
    assert '"mark order_id=" + ИдЗаказа + " %"' in f


def test_supply_quantity_must_be_a_positive_integer():
    f = _function("РазобратьПозицииПоставки")
    assert "Поз.Нужно <= 0 ИЛИ Поз.Нужно <> Цел(Поз.Нужно)" in f
