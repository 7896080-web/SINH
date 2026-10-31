"""На одной карточке площадки — товары 1С с РАЗНЫМ цветоразмером.

Сложение остатков по ячейке (`app/stock_cell.py`) держится на посылке: жильцы
одной карточки — это одна и та же физическая вещь. Для партий одной модели так
и есть: `4033` идёт M–3XL, `4052` — 3XL–6XL, на 3XL карточка у них общая, и
покупателю всё равно, из какой партии ему отгрузят. Сумма там ВЕРНА, и находки
по ней быть не должно — 29.10 я дважды написал обратное, и оба текста пришлось
убирать.

А вот если у жильцов разошёлся ЦВЕТОРАЗМЕР — посылка неверна, а сумма всё
равно уедет: карточка продаёт больше, чем лежит по любой из строк. Это тот же
признак, которым программа сопоставляет баркоды (цветоразмер плюс пул), и его
нарушение внутри одной ячейки означает, что сопоставление разошлось с
действительностью.

Прочтений три, следствие у всех одно, а лекарства разные: разные вещи на одной
карточке (отвязать баркод), каталог WB без chrtId (вся карточка стала одной
ячейкой — загрузить каталог заново), одна вещь с разной записью цвета в 1С.
Проверки тут про СЛЕДСТВИЕ: кричит находка или молчит, и то ли число стоит в
строке, которое уйдёт наружу.
"""
import pytest

from app.models import (Barcode, Platform, PlatformCatalogItem, Product,
                        SyncSetting)
from app.report import (CRITICAL, FULL_LISTS, _check_mixed_cells,
                        _rows_mixed_cells)
from tests.factories import make_account

CELL = "177:4242"


def _item(db, account, uid, barcode, *, size="3XL", color="SIYAH", stock=10,
          cell=CELL, enabled=True, broadcast=True, threshold=0, article=None):
    db.add(Product(uid_1c=uid, article=article or uid, name="Куртка",
                   size=size, color=color, stock_on_hand=stock,
                   broadcast_enabled=broadcast,
                   recalc_account_ids=str(account.id)))
    db.add(Barcode(barcode=barcode, uid_1c=uid))
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=enabled,
                       min_threshold=threshold))
    db.add(PlatformCatalogItem(account_id=account.id, external_id=cell,
                               barcode=barcode, article="WB", name="Карточка"))
    db.commit()


def _account(db):
    return make_account(db, Platform.wb, name="ИП КАРАМАН", warehouse_id="wh-1")


def test_two_different_items_on_one_card_are_found(db):
    """Разный РАЗМЕР у жильцов одной ячейки — складываются разные вещи."""
    account = _account(db)
    _item(db, account, "u-3xl", "2000000000011", size="3XL", stock=10)
    _item(db, account, "u-m", "2000000000022", size="M", stock=18)

    finding = _check_mixed_cells(db)

    assert finding is not None
    assert finding.level == CRITICAL
    assert finding.count == 1
    assert finding.link == "/report/rows/mixed_cells"
    assert "mixed_cells" in FULL_LISTS


def test_two_batches_of_one_model_are_silent(db):
    """ГЛАВНАЯ обратная проверка: штатная расстановка находкой НЕ считается.

    4033 M–3XL и 4052 3XL–6XL делят 3XL, цветоразмер у них один, сопоставление
    верное, править нечего. Закричи находка здесь — её выключат вместе со всеми
    настоящими.
    """
    account = _account(db)
    _item(db, account, "u-4033", "2000000000011", size="3XL", stock=10)
    _item(db, account, "u-4052", "2000000000022", size="3XL", stock=18)

    assert _check_mixed_cells(db) is None


def test_case_and_spaces_are_not_a_difference(db):
    """«siyah» и « SIYAH » — одно и то же, и ругаться на это нельзя."""
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", size="3XL", color="SIYAH")
    _item(db, account, "u-b", "2000000000022", size=" 3xl ", color="siyah")

    assert _check_mixed_cells(db) is None


def test_a_different_color_at_the_same_size_is_found(db):
    """Цвет — половина цветоразмера, и по нему программа тоже сопоставляет."""
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", color="SIYAH")
    _item(db, account, "u-b", "2000000000022", color="BRICK RED")

    assert _check_mixed_cells(db) is not None


@pytest.mark.parametrize("field", ["size", "color"])
def test_an_empty_size_or_color_is_not_judged(db, field):
    """Судить не по чему — молчим, а не кричим.

    Ложная КРИТИЧНАЯ находка будит человека ночью и обесценивает канал целиком,
    а цена молчания здесь ниже: сумма по ячейке видна в строке «Товаров».
    """
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", size="3XL", color="SIYAH")
    _item(db, account, "u-b", "2000000000022", **{field: ""})

    assert _check_mixed_cells(db) is None


def test_a_cell_we_never_send_to_is_silent(db):
    """Наружу по такой ячейке не уходит ничего — оверселла нет, находки нет."""
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", size="3XL", enabled=False)
    _item(db, account, "u-b", "2000000000022", size="M", enabled=False)

    assert _check_mixed_cells(db) is None


def test_one_live_tenant_is_enough(db):
    """А достаточно ОДНОГО живого: его число и уедет на карточку.

    Спроси отбор «все ли живые», находка молчала бы ровно в том случае, что
    разбирали 29.10: у одной партии галочки нет, у второй есть.
    """
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", size="3XL", enabled=True)
    _item(db, account, "u-b", "2000000000022", size="M", enabled=False)

    assert _check_mixed_cells(db) is not None


def test_a_lone_tenant_is_not_a_cell(db):
    """Одиночных карточек — весь каталог, и находка по ним невозможна."""
    account = _account(db)
    _item(db, account, "u-a", "2000000000011")

    assert _check_mixed_cells(db) is None


def test_neighbours_of_other_cells_are_not_mixed(db):
    """Жильцами делает ОДНА ячейка, а не соседство в каталоге."""
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", size="3XL")
    _item(db, account, "u-b", "2000000000022", size="M", cell="177:9999")

    assert _check_mixed_cells(db) is None


def test_the_whole_size_range_in_one_wb_cell_is_found(db):
    """Второе прочтение: каталог WB без chrtId.

    `wb._parse_wb_cards` собирает `external_id` как `nmID:chrtID`, и без chrtID
    выходит `nmID:` — то есть ячейкой становится ВСЯ карточка, а в неё попадает
    весь размерный ряд. Сумма ряда уедет на один баркод; это настоящий
    оверселл, и молчать про него нельзя, хотя мэппинг тут не виноват.
    """
    account = _account(db)
    _item(db, account, "u-m", "2000000000011", size="M", cell="177:")
    _item(db, account, "u-3xl", "2000000000022", size="3XL", cell="177:")

    assert _check_mixed_cells(db) is not None


def test_the_row_names_the_number_that_goes_out(db):
    """В строке стоит ровно то число, которое отправит рассылка.

    Порог кабинета у второго жильца выше его остатка, значит его вклад — ноль,
    и «уходит» обязано быть 10, а не 28. Повтори список сложение сам, он
    обещал бы число, которого на площадке не будет.
    """
    account = _account(db)
    _item(db, account, "u-a", "2000000000011", size="3XL", stock=10,
          article="4033")
    _item(db, account, "u-b", "2000000000022", size="M", stock=18,
          threshold=50, article="4052")

    rows = _rows_mixed_cells(db)

    assert len(rows) == 1
    account_name, cell, articles, sizes, colors, stocks, goes = rows[0]
    assert account_name == "ИП КАРАМАН" and cell == CELL
    assert articles == "4033, 4052"
    assert sizes == "3XL, M"
    assert stocks == "10, 18"
    assert goes == "10", "вклад жильца под порогом кабинета — ноль"


def test_the_finding_and_the_list_see_the_same_rows(db):
    """Разойдись отбор находки и списка, список показывал бы не то, что
    насчитала находка."""
    account = _account(db)
    for i, size in enumerate(("3XL", "M", "L")):
        _item(db, account, f"u-{i}", f"200000000001{i}", size=size,
              cell=f"177:{i // 2}")

    finding = _check_mixed_cells(db)

    assert finding is not None
    assert finding.count == len(_rows_mixed_cells(db))


def test_the_limit_is_named_not_silent(db, monkeypatch):
    """Упёрлись в предел — говорим вслух.

    Молчаливая обрезка читается как «вот и всё, что нашлось», и человек
    разберёт первые строки и успокоится.
    """
    import app.stock_cell as sc

    monkeypatch.setattr(sc, "MIXED_CELL_LIMIT", 1)
    account = _account(db)
    for i in range(3):
        _item(db, account, f"u-a{i}", f"20000000000{i}1", size="3XL",
              cell=f"177:{i}")
        _item(db, account, f"u-b{i}", f"20000000000{i}2", size="M",
              cell=f"177:{i}")

    finding = _check_mixed_cells(db)

    assert finding is not None
    assert "их больше" in finding.title, finding.title


def test_the_finding_is_silent_on_a_healthy_catalogue(db):
    """Молчание на исправной системе — обязательное свойство отчёта."""
    account = _account(db)
    for i, size in enumerate(("S", "M", "L", "XL")):
        _item(db, account, f"u-{i}", f"200000000001{i}", size=size,
              cell=f"177:{i}")

    assert _check_mixed_cells(db) is None
