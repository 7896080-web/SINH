"""Находка «порог не сходится с расхождением и бронью».

Главное число всего механизма — `порог = расхождение + бронь`, и наружу уходит
«остаток ЦС минус порог». 23.09 его порча стоила 62 товаров и 418 лишних штук в
продаже, причём увидеть это было неоткуда: строка показывала согласованные
числа, отчёт молчал, а на площадки уже уехал завышенный остаток.

Проверяем здесь три вещи, и вторая из них важнее первой:

  * порог, записанный мимо формулы, находится и называется числами;
  * на исправной системе находка МОЛЧИТ — включая те состояния, где формула
    намеренно не имеет ответа (ручной порог без расхождения и без даты, строка
    после «Сбросить порог»);
  * следствие названо НАПРАВЛЕНИЕМ: порог меньше формулы — оверселл, больше —
    товар недопродаётся. Одно следствие на оба случая было бы неправдой ровно
    в половине строк.
"""

from app.models import Product
from app.offset_base import clear_offset
from app.report import CRITICAL, _q_offset_disagrees, collect_findings


def _product(db, uid="u-1", *, article="ART", size="M", stock=50, reserve=2,
             discrepancy=11, offset=13, broadcasting=True, base_date=None,
             base_stock=None, fact=None):
    """Товар с явно заданными порогом и слагаемыми.

    Порог ставится ПРЯМО, а не через `recompute_offset`: предмет проверки —
    именно расхождение хранимого порога с формулой, и получить его штатным
    путём нельзя, он для того и написан.
    """
    p = Product(uid_1c=uid, article=article, size=size, name="Свитшот",
                stock_on_hand=stock, reserve=reserve,
                stock_discrepancy=discrepancy, broadcast_offset=offset,
                broadcast_enabled=broadcasting, offset_base_date=base_date,
                offset_base_stock=base_stock, fact_at_date=fact)
    db.add(p)
    db.commit()
    return p


def _finding(db):
    return next((f for f in collect_findings(db) if f.key == "offset_disagrees"), None)


# ------------------------------------------------------------------ тишина

def test_a_consistent_offset_is_not_a_finding(db):
    """Порог равен расхождению плюс броне — сказать нечего."""
    _product(db, discrepancy=11, reserve=2, offset=13)

    assert _q_offset_disagrees(db) == []
    assert _finding(db) is None


def test_a_hand_written_offset_without_a_measurement_is_left_alone(db):
    """Расхождения не измеряли, даты нет — формула ответа не имеет, и там живёт
    число, введённое руками. Спорить с ним не о чем; объяви мы это находкой,
    отчёт покраснел бы у каждой строки, настроенной до появления колонки."""
    _product(db, discrepancy=None, offset=7, base_date=None, base_stock=None)

    assert _finding(db) is None


def test_a_reset_row_is_not_a_finding(db):
    """«Сбросить порог» снимает и порог, и расхождение, и все исходные числа.
    Формула после него молчит — значит и находка обязана."""
    p = _product(db, discrepancy=11, reserve=2, offset=13)
    clear_offset(db, p, username="tester")
    db.commit()

    assert _finding(db) is None


def test_a_silent_product_is_not_a_finding(db):
    """Трансляция выключена: неверный порог наружу пока не уходит, следствия
    нет. Находка без следствия здесь не находка — она приучает пролистывать
    отчёт целиком, и тогда он бесполезен весь."""
    _product(db, discrepancy=11, reserve=2, offset=99, broadcasting=False)

    assert _finding(db) is None


def test_a_row_without_an_offset_at_all_is_not_a_finding(db):
    """Дата задана, 1С ответила, расхождения ещё не измеряли, порога нет.

    Формула на такой строке даёт ровно бронь, а лестница без порога отдаёт
    «остаток минус бронь» — то есть ТО ЖЕ САМОЕ число. Расхождения между
    столбцами нет, есть разница в способе записи, и объявлять её находкой
    значит зажечь красное у каждой настроенной, но ещё не измеренной строки:
    отчёт, красный всегда, читать перестают.
    """
    import datetime

    _product(db, discrepancy=None, reserve=3, offset=None,
             base_date=datetime.date(2026, 8, 7), base_stock=40)

    assert _finding(db) is None


def test_the_legacy_branch_agrees_with_the_formula(db):
    """Строка без измеренного расхождения, но с базовой датой: формула идёт
    старой веткой и даёт ровно бронь. Порог, равный броне, — согласован."""
    _product(db, discrepancy=None, reserve=3, offset=3,
             base_date=__import__("datetime").date(2026, 8, 7), base_stock=40)

    assert _finding(db) is None


# ------------------------------------------------------------------ находка

def test_an_offset_written_past_the_formula_is_found(db):
    """Порог 0 при расхождении 11 и броне 2 — ровно случай 23.09."""
    _product(db, uid="u-порча", article="27643", discrepancy=11, reserve=2,
             offset=0, stock=22)

    found = _finding(db)

    assert found is not None
    assert found.level == CRITICAL
    assert found.count == 1
    # Числа в подробностях обязаны быть: «порог не сходится» без них человек
    # пролистывает, потому что проверить нечем.
    detail = found.details[0]
    assert "27643" in detail
    assert "11" in detail and "2" in detail and "13" in detail


def test_the_consequence_names_the_direction(db):
    """Порог МЕНЬШЕ формулы — наружу уходит больше, чем есть, то есть прямой
    оверселл, и находка обязана назвать его числом. Одно следствие на оба
    направления было бы неправдой ровно в половине строк."""
    _product(db, uid="u-мало", discrepancy=11, reserve=2, offset=0, stock=22)

    found = _finding(db)

    # уходит 22 вместо 9 — лишних 13
    assert "13" in found.consequence
    assert "БОЛЬШЕ" in found.consequence


def test_an_offset_larger_than_the_formula_is_not_counted_as_oversell(db):
    """Обратное направление тоже находка, но следствие у него другое: товар
    недопродаётся. В счёт «лишних штук» он попасть не должен."""
    _product(db, uid="u-много", discrepancy=11, reserve=2, offset=40, stock=50)

    found = _finding(db)

    assert found is not None
    assert "БОЛЬШЕ" not in found.consequence


def test_the_row_carries_both_numbers(db):
    """Разобрать строку можно, только видя оба числа рядом — что уходит сейчас
    и что ушло бы по формуле."""
    _product(db, uid="u-строка", discrepancy=11, reserve=2, offset=0, stock=22)

    row = _q_offset_disagrees(db)[0]

    assert (row.offset_now, row.offset_by_formula) == (0, 13)
    assert (row.goes_now, row.goes_by_formula) == (22, 9)


def test_the_full_list_page_opens(logged_in_client, web_db):
    """Находка обещает полный список — страница обязана существовать и
    показывать те же числа. Якорь на несуществующий список — немой отказ."""
    _product(web_db, uid="u-веб", article="27643", discrepancy=11, reserve=2,
             offset=0, stock=22)

    page = logged_in_client.get("/report/rows/offset_disagrees").text

    assert "27643" in page
    assert "Порог по формуле" in page
    assert "Ушло бы по формуле" in page


def test_the_finding_and_the_list_count_the_same_thing(db):
    """Находка и список обязаны считать ОДНО И ТО ЖЕ: разойдись они, список
    показывал бы не то, что насчитала находка."""
    from app.report import FULL_LISTS

    for i in range(3):
        _product(db, uid=f"u-{i}", article=f"A{i}", discrepancy=5, reserve=1,
                 offset=0, stock=30)

    _, _, rows_fn = FULL_LISTS["offset_disagrees"]

    assert _finding(db).count == len(rows_fn(db))
