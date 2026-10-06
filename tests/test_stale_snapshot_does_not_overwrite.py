"""Старый снимок не затирает свежую дельту — иначе час идёт оверселл.

Разбор 06.10 по `2617 C24-2317CQ BRICK RED` размер 52, по журналу сверки с
точностью до секунды:

    12:29:39  в 1С перемещение ЦБ000002292 увезло 4 штуки: 15 -> 11
    12:29:58  дельта принесла 11 — применили, у нас 11
    12:33:05  ЧАСОВОЙ СНИМОК принёс 15 — применили, у нас снова 15
    13:33:05  следующий снимок принёс 11 — применили, у нас 11

Снимок принёс 15 не по ошибке: 1С сформировала тот файл ДО перемещения, а
применяем мы его позже. То есть старые данные затёрли новые, и ЦЕЛЫЙ ЧАС на
площадку уходило 15 при реальных 11 — прямой оверселл, по десятку позиций
сразу (одно перемещение трогает много строк). Со стороны это выглядит
качанием остатка туда-обратно, и по итоговой разнице причина неотличима от
зависшего задания 1С.

Защита по времени ЗАПРОСА выгрузки (`fetch_stock_export_snapshot`) этот случай
не ловит и не может: файл НОВЕЕ запроса, он просто старше дельты.

Проверяем по СЛЕДСТВИЮ — по остатку товара после применения, а не по тому,
вызвана ли функция: предмет правки — число, которое уедет на площадку.

Сравнение строго СТАРШЕ (`<`), а не «старше или равно», и тестом эта граница НЕ
закрыта намеренно: при равных временах речь идёт о повторном применении того же
файла, а оно уже произошло в той же транзакции — наблюдаемой разницы между `<` и
`<=` нет. Писать проверку, которая ловила бы мутацию, но не описывала бы ни
одного настоящего случая, значит заводить тест, стерегущий пустоту.
"""
from datetime import datetime, timedelta

from app.models import Barcode, Product
from app.workers.reconciliation import run_reconciliation

BARCODE = "2000932307435"
MOVEMENT = datetime(2026, 10, 5, 12, 29, 39)   # перемещение в 1С (UTC)
DELTA_AT = MOVEMENT + timedelta(seconds=19)    # файл дельты
SNAPSHOT_BEFORE = MOVEMENT - timedelta(minutes=5)   # выгрузка сформирована ДО
SNAPSHOT_AFTER = MOVEMENT + timedelta(hours=1)      # следующий час


def _product(db, quantity: int = 15) -> Product:
    db.add(Product(uid_1c="u-52", article="2617 C24-2317CQ", size="52",
                   color="BRICK RED", name="Куртка тинсулейт",
                   stock_on_hand=quantity))
    db.add(Barcode(barcode=BARCODE, uid_1c="u-52"))
    db.commit()
    return db.query(Product).filter(Product.uid_1c == "u-52").first()


def _stock(db) -> int:
    db.expire_all()
    return db.query(Product).filter(Product.uid_1c == "u-52").first().stock_on_hand


def test_a_snapshot_older_than_the_delta_is_ignored(db):
    """Главное следствие: число остаётся тем, что принесла дельта."""
    _product(db, 15)

    # Дельта: частичный файл, обнулять отсутствующее нельзя.
    run_reconciliation(db, {BARCODE: 11}, missing_means_zero=False,
                       snapshot_at=DELTA_AT)
    db.commit()
    assert _stock(db) == 11

    # Часовой снимок, сформированный ДО перемещения.
    stats = run_reconciliation(db, {BARCODE: 15}, missing_means_zero=True,
                               snapshot_at=SNAPSHOT_BEFORE)
    db.commit()

    assert _stock(db) == 11, (
        "снимок, сформированный раньше дельты, затёр свежее число — "
        "ровно инцидент 06.10, час оверселла на живой карточке")
    assert stats["stale_rows"] == 1, (
        "строку пропустили молча: у счётчика непроведённой работы обязан быть "
        "читатель")


def test_the_next_snapshot_still_applies(db):
    """Самолечение обязано сохраниться: свежий снимок — хозяин.

    Отбрасывай мы снимок навсегда, одна дельта замораживала бы строку: часовая
    выгрузка — единственный канал, который ловит изменения, прошедшие мимо
    дельты, и потеряв его, мы потеряли бы и обнуление распроданного.
    """
    _product(db, 15)
    run_reconciliation(db, {BARCODE: 11}, missing_means_zero=False,
                       snapshot_at=DELTA_AT)
    run_reconciliation(db, {BARCODE: 15}, missing_means_zero=True,
                       snapshot_at=SNAPSHOT_BEFORE)
    db.commit()
    assert _stock(db) == 11

    run_reconciliation(db, {BARCODE: 9}, missing_means_zero=True,
                       snapshot_at=SNAPSHOT_AFTER)
    db.commit()

    assert _stock(db) == 9, "свежий снимок обязан применяться как раньше"


def test_a_snapshot_without_a_time_works_as_before(db):
    """Нет времени файла — работаем по-прежнему, а не блокируем всё.

    `file_mtime_utc` может вернуть None (файловая система не отдала время).
    Прими мы это за «данные неизвестной свежести» и начни отбрасывать, сверка
    встала бы целиком — а это единственный канал, которым приходит остаток.
    """
    _product(db, 15)
    run_reconciliation(db, {BARCODE: 11}, missing_means_zero=False,
                       snapshot_at=DELTA_AT)
    db.commit()

    run_reconciliation(db, {BARCODE: 7}, missing_means_zero=True,
                       snapshot_at=None)
    db.commit()

    assert _stock(db) == 7


def test_an_untouched_product_takes_any_snapshot(db):
    """У строки без отметки времени (`NULL`) снимок применяется всегда.

    Так стоит у всего каталога до этой колонки: NULL значит «не знаем, на какой
    момент», и отбрасывать по нему нельзя — иначе после наката сверка перестала
    бы работать на всех 152 тысячах строк разом.
    """
    _product(db, 15)

    run_reconciliation(db, {BARCODE: 4}, missing_means_zero=True,
                       snapshot_at=SNAPSHOT_BEFORE)
    db.commit()

    assert _stock(db) == 4


def test_a_matching_snapshot_still_stamps_the_time(db):
    """Снимок, по которому разницы НЕТ, время всё равно проставляет.

    Иначе так: снимок сошёлся (значит данные подтверждены на его момент), потом
    приходит файл ПОСТАРШЕ и спокойно применяется — он ведь «первый, кто
    отметился». Отметка нужна на любом исходе, не только на расхождении.
    """
    product = _product(db, 11)

    run_reconciliation(db, {BARCODE: 11}, missing_means_zero=True,
                       snapshot_at=SNAPSHOT_AFTER)
    db.commit()
    db.expire_all()
    product = db.query(Product).filter(Product.uid_1c == "u-52").first()
    assert product.stock_as_of == SNAPSHOT_AFTER

    run_reconciliation(db, {BARCODE: 15}, missing_means_zero=True,
                       snapshot_at=SNAPSHOT_BEFORE)
    db.commit()

    assert _stock(db) == 11, "старый снимок применился после сошедшегося"


def test_a_stale_snapshot_does_not_zero_a_sold_out_row(db):
    """И обнуление отсутствующего в снимке — тот же случай, не особый.

    Самый дорогой исход у обнуления: остаток уходит в ноль, карточка перестаёт
    продавать, а исправиться само это не может — остатка больше нет, значит
    следующего события по строке не будет вовсе. Старый снимок, в котором
    товара ещё не было, обнулил бы то, что дельта минуту назад пополнила.
    """
    _product(db, 0)
    run_reconciliation(db, {BARCODE: 12}, missing_means_zero=False,
                       snapshot_at=DELTA_AT)
    db.commit()
    assert _stock(db) == 12

    # В старом снимке этой строки нет вовсе — «распродана в ноль».
    run_reconciliation(db, {"7700000000001": 3}, missing_means_zero=True,
                       snapshot_at=SNAPSHOT_BEFORE)
    db.commit()

    assert _stock(db) == 12, (
        "старый снимок обнулил строку, пополненную свежей дельтой")

