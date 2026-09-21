"""Справочник 1С перебивает ДОГАДКУ автопривязки — и только её.

`catalog_sync` привязывает незнакомый баркод к товару, если его соседи по
карточке площадки ведут к одному товару 1С (`pool_match`). Догадка полезная, но
это не первичный учёт: баркод заводится в карточке 1С, и если данные разошлись,
права 1С.

Приём справочника не менял существующие привязки НИКОГДА, поэтому ошибочная
догадка жила вечно: заказ по такому баркоду списывался с ЧУЖОГО товара — у него
остаток падал зря, а у настоящего оставался завышенным и уезжал на площадки.
Конфликт сопоставления при этом был удалён автопривязкой, и разбирать было
нечего — то есть увидеть это было неоткуда.
"""
from app.models import Barcode, Product
from app.timeutils import now_utc
from app.workers.reconciliation import import_barcode_dict


def _product(db, uid, **kw):
    db.add(Product(uid_1c=uid, article=f"A-{uid}", name="Товар", stock_on_hand=1, **kw))
    db.commit()


def test_a_guess_is_corrected_by_the_dictionary(db):
    _product(db, "u1", recalc_done_at=now_utc(), recalc_account_ids="1")
    _product(db, "u2")
    db.add(Barcode(barcode="b1", uid_1c="u1", source_platform="pool_match"))
    db.commit()

    stats = import_barcode_dict(db, [{"uid_1c": "u2", "barcode": "b1"}], full=True)

    assert stats["repointed_guesses"] == 1
    assert db.query(Barcode).filter(Barcode.barcode == "b1").one().uid_1c == "u2"


def test_the_correction_drops_the_mark_on_both_products(db):
    """Расчёт собирал заказы по ПРЕЖНЕМУ набору баркодов — у обоих товаров его
    вывод больше не относится к делу."""
    _product(db, "u1", recalc_done_at=now_utc(), recalc_account_ids="1")
    _product(db, "u2", recalc_done_at=now_utc(), recalc_account_ids="1")
    db.add(Barcode(barcode="b1", uid_1c="u1", source_platform="pool_match"))
    db.commit()

    import_barcode_dict(db, [{"uid_1c": "u2", "barcode": "b1"}], full=True)

    for uid in ("u1", "u2"):
        product = db.query(Product).filter(Product.uid_1c == uid).one()
        assert product.recalc_done_at is None, uid
        assert product.recalc_account_ids == "", uid


def test_a_human_repoint_is_never_overridden(db):
    """Ручная переподвязка делается под отдельной галочкой «разрешить
    переподвязку» — это осознанное решение человека, и молча его перебивать
    нельзя: старый файл справочника переразнёс бы каталог обратно."""
    _product(db, "u1")
    _product(db, "u2")
    db.add(Barcode(barcode="b1", uid_1c="u1", source_platform="excel_repoint"))
    db.commit()

    stats = import_barcode_dict(db, [{"uid_1c": "u2", "barcode": "b1"}], full=True)

    assert stats["repointed_guesses"] == 0
    assert db.query(Barcode).filter(Barcode.barcode == "b1").one().uid_1c == "u1"


def test_a_matching_guess_is_left_alone(db):
    """Догадка, которая совпала со справочником, — не повод что-либо трогать:
    иначе каждый прогон снимал бы «актуализирован» по всему каталогу."""
    _product(db, "u1", recalc_done_at=now_utc())
    db.add(Barcode(barcode="b1", uid_1c="u1", source_platform="pool_match"))
    db.commit()

    stats = import_barcode_dict(db, [{"uid_1c": "u1", "barcode": "b1"}], full=True)

    assert stats["repointed_guesses"] == 0
    assert db.query(Product).filter(Product.uid_1c == "u1").one().recalc_done_at is not None


def test_the_correction_is_written_to_the_journal(db):
    """Мы меняем, на какой товар спишется заказ. Такое не делается молча."""
    from app.models import AuditLog

    _product(db, "u1")
    _product(db, "u2")
    db.add(Barcode(barcode="b1", uid_1c="u1", source_platform="pool_match"))
    db.commit()

    import_barcode_dict(db, [{"uid_1c": "u2", "barcode": "b1"}], full=True)

    entry = db.query(AuditLog).filter(AuditLog.action == "barcode_guess_corrected").one()
    assert "b1" in entry.details and "u1" in entry.details and "u2" in entry.details
