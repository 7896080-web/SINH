"""Сдвиг даты НАЗАД сохраняет уже установленный порог.

Дата расчёта служит двум разным целям сразу: от неё считается порог (дата +
остаток ЦС на дату + факт) и от неё же расчёт поднимает заказы площадок. Чтобы
догнать продажи за более ранний период, дату приходится двигать назад — и вместе
с заказами терялся порог: факт на прежнюю дату справедливо стирается, а без
факта формула даёт просто бронь. То есть работа, сделанная СВЕЖИМ физическим
пересчётом склада, пропадала ради того, чтобы поднять старые заказы.
"""
from datetime import date

import pytest

from app.models import (Product, StockDateRow, StockDateSnapshot, StockDateStatus)
from app.offset_base import fill_waiting_products, set_base_date
from app.transmit import offset_from_base


def _snapshot(db, day, rows):
    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done,
                             rows_count=len(rows))
    db.add(snap)
    db.flush()
    for uid, qty in rows:
        db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    db.commit()
    db.refresh(snap)
    return snap


def _product(db, **kw):
    p = Product(uid_1c="u1", article="A-1", name="Товар", stock_on_hand=10,
                broadcast_enabled=True, **kw)
    db.add(p)
    db.commit()
    return p


# --------------------------------------------------------------------------
# Случай оператора, ради которого всё и делалось
# --------------------------------------------------------------------------

def test_the_threshold_survives_a_step_back_in_time(db):
    """Сценарий с боя, в его собственных числах.

    Расчёт на 10.09: 1С показала 10, физически пересчитали 5 — порог 5. Теперь
    нужен расчёт с 10.08, чтобы поднять заказы за месяц, и порог 5 обязан
    остаться: он получен настоящим пересчётом склада, и более точного числа у
    нас нет и не будет — склад в прошлом не пересчитать.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    from app.transmit import recompute_offset
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 5, "порог по пересчёту 10.09"

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.offset_base_date == date(2026, 8, 10)
    assert product.broadcast_offset == 5, "порог обязан пережить сдвиг назад"
    # Факт подобран под новую дату: при том же расхождении в 5 единиц на 10.08
    # при учётных 30 на складе лежало бы 25.
    assert product.fact_at_date == 25
    assert offset_from_base(product) == 5, "связка снова согласована"


def test_the_recalc_mark_still_goes_away(db):
    """Заказы за добавившийся период обязаны быть подняты заново.

    Сохранение порога не отменяет главного: расчёт проводил заказы ОТ ПРЕЖНЕЙ
    даты, и к новому периоду его вывод не относится. Оставь мы отметку — остаток
    уехал бы наружу завышенным ровно на непроведённые продажи.
    """
    from app.timeutils import now_utc

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db, recalc_done_at=now_utc(), recalc_account_ids="1")

    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    product.recalc_done_at = now_utc()
    product.recalc_account_ids = "1"
    db.commit()

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.recalc_done_at is None
    assert product.recalc_account_ids == ""


def test_a_reserve_change_still_moves_the_threshold(db):
    """Главная причина подбирать ФАКТ, а не просто «запретить пересчёт».

    Запрети мы пересчёт — три исходных числа перестали бы соответствовать
    сохранённому порогу, и первая же правка брони вернула бы его к броне молча.
    Бронь меняют чаще всего остального, так что ждать пришлось бы недолго.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db, reserve=0)

    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    recompute_offset(product)
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.broadcast_offset == 5

    product.reserve = 3
    recompute_offset(product)
    db.commit()

    assert product.broadcast_offset == 8, "порог сдвинулся ровно на изменение брони"


# --------------------------------------------------------------------------
# Границы
# --------------------------------------------------------------------------

def test_moving_forward_still_clears_the_fact(db):
    """Вперёд дату двигают ПОСЛЕ нового пересчёта склада.

    Там верно прежнее правило: факт всегда «на дату», старое число к новому
    отношения не имеет. Сохранять порог здесь значило бы отменить работу,
    которую оператор как раз и пришёл сделать.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)

    set_base_date(db, product, date(2026, 8, 10))
    product.fact_at_date = 25
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 5

    set_base_date(db, product, date(2026, 9, 10))
    db.commit()

    assert product.fact_at_date is None
    assert product.broadcast_offset == 0, "10 − (10 − 0): сводится к брони"


def test_nothing_to_keep_when_the_warehouse_was_never_counted(db):
    """Порог без факта равен просто броне — удерживать нечего.

    Формально он «установлен», но поставил его не человек, а формула при
    отсутствии факта. Подставить такой строке выведенный факт значило бы создать
    видимость физического пересчёта, которого не было, — а на новой дате порог и
    так выйдет тем же самым.
    """
    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    product = _product(db)

    set_base_date(db, product, date(2026, 9, 10))
    db.commit()
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()

    assert product.fact_at_date is None
    assert product.offset_pinned is None


# --------------------------------------------------------------------------
# Отложенный случай: снимка на новую дату ещё нет
# --------------------------------------------------------------------------

def test_the_threshold_is_kept_even_when_1c_has_not_answered_yet(db):
    """Самый частый случай на бою: срез на старую дату ещё не заказан.

    Порог переживает саму смену даты сам собой (без остатка формула не
    считается), а вот через час, когда придёт ответ 1С, он тихо сменился бы на
    бронь — то есть уже после того, как оператор увидел, что всё в порядке.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 9, 10), [("u1", 10)])
    product = _product(db)
    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 5
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 5

    # Снимка на 10.08 ещё нет — строка встаёт в ожидание.
    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    assert product.offset_base_stock is None
    assert product.offset_pinned == 5, "намерение записано"
    assert product.broadcast_offset == 5

    snap = _snapshot(db, date(2026, 8, 10), [("u1", 30)])
    stats = fill_waiting_products(db, snap)

    assert stats["offsets_kept"] == 1
    assert product.broadcast_offset == 5
    assert product.fact_at_date == 25
    assert product.offset_pinned is None, "намерение снято, второй раз не сработает"


def test_an_impossible_threshold_is_reported_not_swallowed(db):
    """Подобранный факт был бы отрицательным — прежний порог на эту дату невозможен.

    Молча оставить порог нельзя (он перестал бы соответствовать трём числам), и
    молча сменить тоже: оператор просил сохранить. Поэтому такие строки
    считаются отдельно.
    """
    from app.transmit import recompute_offset

    _snapshot(db, date(2026, 9, 10), [("u1", 100)])
    product = _product(db, reserve=0)
    set_base_date(db, product, date(2026, 9, 10))
    product.fact_at_date = 0
    recompute_offset(product)
    db.commit()
    assert product.broadcast_offset == 100

    set_base_date(db, product, date(2026, 8, 10))
    db.commit()
    snap = _snapshot(db, date(2026, 8, 10), [("u1", 1)])
    stats = fill_waiting_products(db, snap)

    assert stats["offsets_lost"] == 1
    assert stats["offsets_kept"] == 0
    assert product.offset_pinned is None, "не должно звенеть при каждом снимке"


# --------------------------------------------------------------------------
# У счётчика обязан быть читатель
# --------------------------------------------------------------------------

def test_a_threshold_that_could_not_be_kept_reaches_a_human(db):
    """Счётчик считался и выбрасывался — ни лога, ни heartbeat, ни находки.

    То есть человек просил сохранить число, которым управляется отправка,
    система не смогла и не сказала НИКОМУ. Ровно «находка без читателя»,
    которую в этом проекте чинят везде; она была заведена заново вместе с самим
    удержанием, и первый же вопрос с боя («почему порог съехал?») уткнулся в то,
    что ответить по данным нечем.
    """
    from app.models import WorkerHeartbeat
    from app.report import collect_findings
    from app.timeutils import now_utc

    db.add(WorkerHeartbeat(
        worker_name="ftp_receive", last_run_at=now_utc(), last_success=True,
        last_error="порог не удержан при смене даты назад: 3 товаров"))
    db.commit()

    found = [f for f in collect_findings(db) if f.key == "offset_not_kept"]

    assert len(found) == 1
    assert "3 товаров" in found[0].title
    # Следствие называется, а не просто факт: иначе это не расхождение, а число.
    assert "больше" in found[0].consequence.lower()


def test_a_clean_run_says_nothing(db):
    """Молчание на исправной системе — обязательное свойство отчёта."""
    from app.models import WorkerHeartbeat
    from app.report import collect_findings
    from app.timeutils import now_utc

    db.add(WorkerHeartbeat(worker_name="ftp_receive", last_run_at=now_utc(),
                           last_success=True, last_error=None))
    db.commit()

    assert [f for f in collect_findings(db) if f.key == "offset_not_kept"] == []


def test_the_counter_actually_reaches_the_heartbeat(db):
    """Читатель есть, писатель есть — а что они соединены, надо доказать.

    Мутация «убрать `note` из вызова `_heartbeat`» не ловилась ничем: находка
    продолжала читать `last_error`, которого теперь никто не пишет. Ровно тот
    разрыв, ради которого весь этот механизм и заводился, только на один слой
    выше. Поведенческим тестом это стоило бы поднятого обмена с 1С и снимка,
    поэтому проверяем исходник — тем же приёмом, что и сроки протухания заданий
    в `/health`.
    """
    import re
    from pathlib import Path

    source = Path("app/workers/scheduler.py").read_text(encoding="utf-8")
    job = source[source.index("def job_ftp_receive"):]
    job = job[:job.index("\ndef ")]

    assert 'on_date.get("offsets_lost")' in job, (
        "счётчик «порог не удержан» должен читаться из статистики")
    call = re.search(r'_heartbeat\(db,\s*"ftp_receive",\s*True[^)]*\)', job)
    assert call is not None, "успешная отметка ftp_receive не найдена"
    assert "note" in call.group(0), (
        "текст «порог не удержан» обязан уходить в last_error УСПЕШНОЙ отметки — "
        "иначе находка отчёта читает то, чего никто не пишет")


# --------------------------------------------------------------------------
# Строка обязана объяснить, что обнуляет правка факта
# --------------------------------------------------------------------------

def test_the_row_names_the_discrepancy_out_loud(logged_in_client, web_db):
    """23.09 на бою порог удержался, а объяснить это строка не смогла.

    После сдвига даты назад под удержанный порог подобрался факт — 32 при учёте
    43. Оператор этого числа на новую дату не вводил, и «поправил» факт на
    учётное 43. Порог честно стал нулём: факт, равный учёту, означает
    «расхождения нет». Компенсировали бронью, наружу пошло то же число — но
    собранное из другого, и при следующей правке брони они разойдутся.

    Арифметика «43 − (32 − бронь 0)» это показывала, но читается как формула, а
    не как утверждение о складе. Названное вслух расхождение говорит прямо, что
    именно обнуляет правка факта.
    """
    from app.models import Product, StockDateRow, StockDateSnapshot, StockDateStatus

    snap = StockDateSnapshot(snapshot_date=date(2026, 7, 6),
                             status=StockDateStatus.done, rows_count=1)
    web_db.add(snap)
    web_db.commit()
    web_db.refresh(snap)
    web_db.add(StockDateRow(snapshot_id=snap.id, uid_1c="u1", quantity=43))
    web_db.add(Product(uid_1c="u1", article="A-1", name="Товар",
                       stock_on_hand=22, reserve=0, broadcast_enabled=True))
    web_db.commit()

    logged_in_client.post("/products/u1/base-date", data={"value": "2026-07-06"})
    page = logged_in_client.post("/products/u1/fact", data={"value": "32"}).text

    assert "расхождение" in page
    assert "учёт 43 − факт 32" in page
    assert "<b>11</b>" in page, "само число расхождения обязано быть названо"


def test_a_fact_equal_to_the_1c_number_shows_a_zero_discrepancy(logged_in_client, web_db):
    """Обратная сторона — то самое действие, которое на бою и произошло.

    Поставив факт равным учёту, человек утверждает «склад сходится с 1С». Строка
    обязана сказать это в лицо, а не оставить вывод на догадку.
    """
    from app.models import Product, StockDateRow, StockDateSnapshot, StockDateStatus

    snap = StockDateSnapshot(snapshot_date=date(2026, 7, 6),
                             status=StockDateStatus.done, rows_count=1)
    web_db.add(snap)
    web_db.commit()
    web_db.refresh(snap)
    web_db.add(StockDateRow(snapshot_id=snap.id, uid_1c="u1", quantity=43))
    web_db.add(Product(uid_1c="u1", article="A-1", name="Товар",
                       stock_on_hand=22, reserve=0, broadcast_enabled=True))
    web_db.commit()

    logged_in_client.post("/products/u1/base-date", data={"value": "2026-07-06"})
    page = logged_in_client.post("/products/u1/fact", data={"value": "43"}).text

    assert "учёт 43 − факт 43" in page
    assert "<b>0</b>" in page
