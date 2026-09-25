"""Code128: проверка по ИЗВЕСТНЫМ значениям, а не по виду картинки.

Ошибка в контрольной сумме не видна глазами вовсе: штрихкод напечатается,
выглядеть будет как штрихкод, и не прочитается. Узнать об этом можно только
сканером — то есть на складе, на пачке уже наклеенных этикеток, когда наклейку
заново не переклеить. Поэтому здесь эталонные числа из стандарта, а не
«отрисовалось без исключения».
"""

import re

import pytest

from app import barcode128 as bc


def test_the_pattern_table_is_whole():
    """107 шаблонов — ровно столько в стандарте. Сдвиг таблицы на одну строку
    даёт код, который печатается и не читается."""
    assert len(bc.PATTERNS) == 107
    # Все, кроме стоп-символа, — шесть элементов; стоп на один длиннее.
    assert {len(p) for p in bc.PATTERNS[:-1]} == {6}
    assert len(bc.PATTERNS[bc.STOP]) == 7


def test_every_pattern_starts_with_a_bar_and_sums_to_eleven():
    """Каждый символ Code128 — 11 модулей. Опечатка в цифре даёт символ другой
    ширины, и весь код «поедет»: сканер читает по соотношению ширин."""
    for i, p in enumerate(bc.PATTERNS[:-1]):
        assert sum(int(c) for c in p) == 11, f"шаблон {i} шириной не 11"
    assert sum(int(c) for c in bc.PATTERNS[bc.STOP]) == 13


def test_the_known_example_from_the_standard():
    """`A` в наборе B: старт 104, символ 33, сумма (104 + 1*33) % 103 = 34."""
    assert bc.values("A") == [104, 33, 34, 106]


def test_a_real_label_number():
    """`RET-1284` — то, что реально уедет на наклейку. Считано по правилу
    вручную: старт 104, дальше ord-32, взвешенная сумма по модулю 103."""
    codes = bc.values("RET-1284")

    assert codes[0] == bc.START_B
    assert codes[-1] == bc.STOP
    assert codes[1:-2] == [ord(c) - 32 for c in "RET-1284"]

    expected = bc.START_B
    for i, c in enumerate("RET-1284", start=1):
        expected += i * (ord(c) - 32)
    assert codes[-2] == expected % 103


def test_the_checksum_notices_a_swapped_pair():
    """Контрольная сумма ВЗВЕШЕННАЯ, и это её единственный смысл: сумма без
    весов не отличила бы `RET-12` от `RET-21`."""
    assert bc.values("RET-12")[-2] != bc.values("RET-21")[-2]


def test_a_character_outside_set_b_is_refused_out_loud(db=None):
    """Молча выкинуть символ значит напечатать наклейку с ДРУГИМ номером — и
    вещь потом не найдётся вовсе."""
    with pytest.raises(bc.BarcodeError):
        bc.svg("RET-12\n34")


def test_the_svg_keeps_the_bars_crisp():
    """Без `crispEdges` браузер сглаживает края, и на 203 dpi термопринтера
    узкая полоса становится серой — такой код сканер берёт через раз."""
    svg = bc.svg("RET-1284")

    assert 'shape-rendering="crispEdges"' in svg
    assert svg.startswith("<svg") and svg.endswith("</svg>")


def test_the_bars_fill_exactly_the_requested_width():
    """Наклейка 58 мм: вылези код за поле, принтер обрежет крайнюю полосу, и
    код не прочитается — а на глаз он будет выглядеть целым."""
    width = 46.0
    svg = bc.svg("RET-1284", width_mm=width)

    xs = [float(m) for m in re.findall(r'<rect x="([0-9.]+)"', svg)]
    ws = [float(m) for m in re.findall(r'width="([0-9.]+)" height=', svg)]
    assert xs and ws
    assert xs[0] == 0.0
    assert xs[-1] + ws[-1] <= width + 1e-6
    # Code128 всегда кончается полосой, так что правый край и есть ширина кода.
    assert xs[-1] + ws[-1] == pytest.approx(width, abs=1e-3)


def test_the_number_of_bars_matches_the_code():
    """Полосы — ЧЁТНЫЕ элементы шаблона. Сдвиг на один превратил бы полосы в
    пробелы: картинка останется похожей, код станет нечитаемым."""
    codes = bc.values("RET-1284")
    expected = sum(len(bc.PATTERNS[c]) - len(bc.PATTERNS[c]) // 2 for c in codes)

    assert bc.svg("RET-1284").count("<rect") == expected
