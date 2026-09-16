from app.models import Product, Barcode, SyncSetting, PlatformCatalogItem, ProposalSource
from app.workers.catalog_poller import poll_catalog
from tests.factories import make_account


def _seed_catalog_item(db, account, barcode, external_id="ext-1"):
    db.add(PlatformCatalogItem(account_id=account.id, external_id=external_id, barcode=barcode))
    db.commit()


def test_poll_catalog_sets_proposal_for_disabled_product(db):
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()
    _seed_catalog_item(db, account, "111")

    stats = poll_catalog(db, account)

    assert stats["proposed"] == 1
    setting = db.query(SyncSetting).filter(SyncSetting.uid_1c == "u1", SyncSetting.account_id == account.id).first()
    assert setting.has_proposal is True
    assert setting.proposal_source == ProposalSource.catalog_detected


def test_poll_catalog_skips_already_enabled(db):
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=True))
    db.commit()
    _seed_catalog_item(db, account, "111")

    stats = poll_catalog(db, account)

    assert stats["already_enabled"] == 1
    assert stats["proposed"] == 0


def test_poll_catalog_does_not_reset_existing_proposal_date(db):
    account = make_account(db)
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.add(SyncSetting(uid_1c="u1", account_id=account.id, enabled=False, has_proposal=True))
    db.commit()
    _seed_catalog_item(db, account, "111")

    stats = poll_catalog(db, account)

    assert stats["already_proposed"] == 1
    assert stats["proposed"] == 0


def test_poll_catalog_ignores_unrelated_barcodes(db):
    account = make_account(db)
    _seed_catalog_item(db, account, "does-not-exist")
    stats = poll_catalog(db, account)
    assert stats["proposed"] == 0


def test_poll_catalog_no_snapshot_yet_returns_empty_stats(db):
    account = make_account(db)
    stats = poll_catalog(db, account)
    assert stats == {"proposed": 0, "already_enabled": 0, "already_proposed": 0}


def test_poll_catalog_two_wb_cabinets_independent_proposals(db):
    """Одна и та же карточка может быть заведена только в одном из трёх
    кабинетов WB — предложение должно появиться только там."""
    wb1 = make_account(db, name="ИП Яворская")
    wb2 = make_account(db, name="ИП Ребрик")
    db.add(Product(uid_1c="u1", article="A1", name="Товар", stock_on_hand=5))
    db.add(Barcode(barcode="111", uid_1c="u1"))
    db.commit()
    _seed_catalog_item(db, wb1, "111")  # только в первом кабинете

    stats1 = poll_catalog(db, wb1)
    stats2 = poll_catalog(db, wb2)

    assert stats1["proposed"] == 1
    assert stats2["proposed"] == 0  # во втором кабинете эта карточка не найдена
