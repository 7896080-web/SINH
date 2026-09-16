from app.models import Product, Barcode, MappingConflict
from app.workers.matching import resolve_barcode
from tests.factories import make_account


def test_resolve_known_barcode(db):
    p = Product(uid_1c="u1", article="A1", name="Товар 1", stock_on_hand=5)
    db.add(p)
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()
    account = make_account(db)

    result = resolve_barcode(db, "111", account.id)
    assert result == "u1"
    assert db.query(MappingConflict).count() == 0


def test_resolve_unknown_barcode_creates_conflict(db):
    account = make_account(db)
    result = resolve_barcode(db, "999", account.id)
    db.commit()

    assert result is None
    conflict = db.query(MappingConflict).filter(MappingConflict.barcode == "999").first()
    assert conflict is not None
    assert conflict.attempts == 1
    assert conflict.account_id == account.id


def test_repeated_unknown_barcode_increments_attempts(db):
    account = make_account(db)
    resolve_barcode(db, "999", account.id)
    db.commit()
    resolve_barcode(db, "999", account.id)
    db.commit()

    conflict = db.query(MappingConflict).filter(MappingConflict.barcode == "999").first()
    assert conflict.attempts == 2
    assert db.query(MappingConflict).filter(MappingConflict.barcode == "999").count() == 1


def test_same_barcode_different_accounts_are_separate_conflicts(db):
    """Один и тот же баркод может быть неопознан в двух разных кабинетах —
    это два независимых конфликта, не один общий."""
    account1 = make_account(db, name="Кабинет 1")
    account2 = make_account(db, name="Кабинет 2")

    resolve_barcode(db, "999", account1.id)
    db.commit()
    resolve_barcode(db, "999", account2.id)
    db.commit()

    assert db.query(MappingConflict).filter(MappingConflict.barcode == "999").count() == 2
