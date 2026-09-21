"""Смена даты расчёта снимает «актуализирован».

Расчёт поднимает заказы площадок ОТ ЗАДАННОЙ ДАТЫ. Сменили дату — его вывод к
новому периоду не относится, и отметка «актуализирован» становится неправдой.

Видно это было не сразу: смена даты стирает факт, а проверка факта в
`calc_status` стоит раньше проверки расчёта, поэтому строка показывала «нужен
факт». Человек вводил факт — и строка объявлялась «актуализирован» по расчёту от
ПРЕЖНЕЙ даты, то есть ворота трансляции открывались на несверенный остаток.

Опаснее всего сдвиг даты НАЗАД: заказы за добавившийся кусок периода не
проведены, остаток ЦС завышен ровно на них, и трансляция отправит наружу больше,
чем есть на складе.
"""
from datetime import date

from app.broadcast_gate import calc_status
from app.models import Product, StockDateRow, StockDateSnapshot, StockDateStatus
from app.offset_base import set_base_date
from app.timeutils import now_utc

OLD = date(2026, 8, 7)
NEW = date(2026, 7, 1)


def _snapshot(db, day, rows):
    snap = StockDateSnapshot(snapshot_date=day, status=StockDateStatus.done,
                             requested_by="t", received_at=now_utc())
    db.add(snap)
    db.commit()
    for uid, qty in rows:
        db.add(StockDateRow(snapshot_id=snap.id, uid_1c=uid, quantity=qty))
    db.commit()


def _calculated(db):
    """Товар, доведённый до «актуализирован»: дата, снимок, факт, расчёт."""
    product = Product(uid_1c="u1", article="A", name="Товар", stock_on_hand=10,
                      reserve=0, offset_base_date=OLD, offset_base_stock=10,
                      fact_at_date=10, recalc_done_at=now_utc(),
                      recalc_account_ids="1")   # кабинет покрыт расчётом
    db.add(product)
    db.commit()
    return product


def test_a_calculated_row_reads_as_ready(db):
    """Контроль: без смены даты строка действительно «актуализирован» — иначе
    тесты ниже зеленели бы по посторонней причине."""
    product = _calculated(db)
    assert calc_status(product, has_cabinet=True)[0] == "ready"


def test_changing_the_date_drops_the_mark(db):
    product = _calculated(db)
    _snapshot(db, NEW, [("u1", 8)])

    set_base_date(db, product, NEW)

    assert product.recalc_done_at is None


def test_entering_a_fact_does_not_bring_the_mark_back(db):
    """Тот самый путь, которым дефект и проявлялся: факт стёрт сменой даты,
    человек вводит новый — и строка не должна назваться «актуализирован»."""
    product = _calculated(db)
    _snapshot(db, NEW, [("u1", 8)])
    set_base_date(db, product, NEW)

    product.fact_at_date = 8
    db.commit()

    assert calc_status(product, has_cabinet=True)[0] == "need_recalc"


def test_coverage_is_still_tracked(db):
    """Пустая СТРОКА, а не NULL: покрытие аннулировано, но отслеживаем мы его
    по-прежнему. NULL означал бы «расчёта никогда не было», и ступень 2 лестницы
    перестала бы срабатывать вовсе — вместо закрытия ворот правка открыла бы их."""
    product = _calculated(db)
    _snapshot(db, NEW, [("u1", 8)])

    set_base_date(db, product, NEW)

    assert product.recalc_account_ids == ""


def test_setting_the_same_date_changes_nothing(db):
    """Повторная простановка той же даты — не смена периода. Снимать отметку тут
    значило бы гонять расчёт по всему каталогу на ровном месте."""
    product = _calculated(db)
    mark = product.recalc_done_at

    set_base_date(db, product, OLD)

    assert product.recalc_done_at == mark
