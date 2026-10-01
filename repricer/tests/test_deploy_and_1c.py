"""Скрипты установки (BOM, не трогают чужие службы) и обработка 1С (часть mark-3).

Проверки модуля 1С — про исходник в репозитории (`../1c/`); на офисном
компьютере его рядом нет, и они пропускаются.
"""
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
DEPLOY = HERE / "deploy"
MODULE = HERE.parent / "1c" / "ОбменССайтом_МодульОбъекта.txt"
repository_only = pytest.mark.skipif(not MODULE.exists(), reason="исходник 1С есть только в репозитории")


def test_every_russian_ps1_has_a_bom():
    scripts = list(DEPLOY.glob("*.ps1"))
    assert scripts
    for p in scripts:
        raw = p.read_bytes()
        text = raw.decode("utf-8-sig")
        if any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text):
            assert raw.startswith(b"\xef\xbb\xbf"), p.name


def test_scripts_never_touch_other_programs():
    for p in DEPLOY.glob("*.ps1"):
        text = p.read_text(encoding="utf-8-sig")
        for svc in ("sync_admin_web", "sync_admin_worker"):
            assert svc not in text, p.name
        assert "markapp.main:app" not in text, p.name


def _function(name: str) -> str:
    text = MODULE.read_text(encoding="utf-8")
    m = re.search(rf"\n(Функция|Процедура) {name}\(.*?\n(КонецФункции|КонецПроцедуры)", text, re.S)
    assert m, name
    return m.group(0)


@repository_only
def test_price_answers_go_to_their_own_folder():
    f = _function("КаталогОтветов")
    assert 'СтрНачинаетсяС(Метка, "price_")' in f and '"pricing"' in f
    assert 'СтрНачинаетсяС(Метка, "mark_")' in f and '"marking"' in f


@repository_only
def test_module_version_is_mark_3_or_later():
    assert int(re.search(r'Возврат "mark-(\d+)";', _function("ВерсияМодуля")).group(1)) >= 3


@repository_only
def test_cost_file_is_published_before_the_answer():
    text = MODULE.read_text(encoding="utf-8")
    cost = text.index('"cost_" + Метка')
    result = text.index('"result_" + Метка')
    assert cost < result, "программа, увидев result, сразу ищет рядом cost — файл обязан уже лежать"
    assert 'КаталогДляОтветов, "cost_" + Метка' in text


@repository_only
def test_cost_source_is_the_cost_register_only():
    f = _function("ВыгрузитьСебестоимость")
    assert "Пар.РегистрСебестоимости" in f and ".СрезПоследних" in f
    assert "ЦеныНоменклатуры" not in f and "Партии" not in f
    assert 'Пар.Вставить("РегистрСебестоимости", "СебестоимостьНоменклатуры")' in MODULE.read_text(encoding="utf-8")


@repository_only
def test_cost_command_checks_field_count_and_answers_with_its_name():
    text = MODULE.read_text(encoding="utf-8")
    block = text[text.index('ИначеЕсли Команда = "EXPORT_COST_PRICES"'):]
    block = block[:block.index("ИначеЕсли", 10)]
    assert "Поля.Количество() < 2" in block
    assert block.count("|EXPORT_COST_PRICES\"") == 2
