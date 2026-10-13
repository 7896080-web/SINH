"""Проверка скидки WB (priceapp/discount_guard.py): автоакция сменила скидку — вернуть эталонную."""
from priceapp import accounts as acc_mod
from priceapp import discount_guard, settings
from priceapp.models import AuditLog, PlatformItem, ReferenceDiscount
from tests import factories as F


class _Wb:
    def __init__(self, error=False):
        self.calls, self.error = [], error

    def _post(self, url, body):
        self.calls.append((url, body))
        return {"error": True, "errorText": "нельзя"} if self.error else {"error": False}


def _setup(db):
    a = F.account(db, "wb", "ИП 1")
    acc_mod.set_credential(db, a, "token", "t")
    F.item(db, a, "a1", "100", external_id="111:1"); F.item(db, a, "a2", "100", external_id="111:2")
    F.item(db, a, "b1", "200", external_id="222:1")
    prices = {"a1": (10000, 5000), "a2": (9000, 4500), "b1": (8000, 4800)}   # 50% и 40%
    for it in db.query(PlatformItem):
        it.current_price, it.current_sale_price = prices[it.barcode]
    db.commit()
    return a


def _autoaction(db, sale_a1, sale_a2):
    for it in db.query(PlatformItem).filter(PlatformItem.barcode.in_(["a1", "a2"])):
        it.current_sale_price = sale_a1 if it.barcode == "a1" else sale_a2
    db.commit()


def test_without_reference_discounts_nothing_is_sent(db):
    _setup(db)
    wb = _Wb()
    st = discount_guard.run(db, lambda p, c: wb)
    assert st["accounts"] == 0 and wb.calls == []
    assert "adopt-discount" in discount_guard.summary(st)


def test_autoaction_discount_is_returned_and_sale_price_fixed_for_follow(db):
    a = _setup(db)
    assert discount_guard.adopt(db) == {"ИП 1": 2}
    assert {r.nm_id: r.discount for r in db.query(ReferenceDiscount)} == {"111": 50, "222": 40}
    _autoaction(db, 3000, 2700)                         # WB поставил 70% на карточку 111
    wb = _Wb()
    st = discount_guard.run(db, lambda p, c: wb)
    assert st["restored"] == 1 and not st["errors"]
    url, body = wb.calls[0]
    assert url.endswith("/api/v2/upload/task") and body == {"data": [{"nmID": 111, "discount": 50}]}
    db.expire_all()
    # «Kit следует за WB» в этом же опросе видит эталонную цену со скидкой, а не акционную
    assert {i.barcode: i.current_sale_price for i in db.query(PlatformItem)} == {"a1": 5000, "a2": 4500, "b1": 4800}
    log = db.query(AuditLog).filter(AuditLog.action == "discount_restore").one()
    assert "111: 70% -> 50%" in log.details
    assert discount_guard.run(db, lambda p, c: _Wb())["restored"] == 0      # уже на месте — не шлём


def test_refused_upload_leaves_prices_and_reports(db):
    _setup(db)
    discount_guard.adopt(db)
    _autoaction(db, 3000, 2700)
    st = discount_guard.run(db, lambda p, c: _Wb(error=True))
    assert st["restored"] == 0 and "нельзя" in st["errors"][0]
    db.expire_all()
    assert db.query(PlatformItem).filter(PlatformItem.barcode == "a1").one().current_sale_price == 3000


def test_guard_can_be_disabled(db):
    _setup(db)
    discount_guard.adopt(db)
    _autoaction(db, 3000, 2700)
    settings.put(db, settings.DISCOUNT_GUARD_ENABLED, "0")
    db.commit()
    wb = _Wb()
    assert discount_guard.run(db, lambda p, c: wb)["disabled"] is True and wb.calls == []


def test_wb_poll_runs_reference_rule_before_kit_follows(db, monkeypatch):
    """Скидку теперь возвращает правило эталонной цены (эталон — цена после скидки продавца);
    оно обязано идти ДО «Kit следует за WB»."""
    from priceapp.workers import jobs
    _setup(db)
    calls = []
    monkeypatch.setattr(jobs.accounts, "load_prices", lambda db_, a, c: calls.append("prices") or {"truncated": False})
    monkeypatch.setattr(jobs.reference, "run", lambda db_, client_factory=None: calls.append("rule") or
                        {"accounts": 0, "revalued": 0, "restored": 0, "eaten": 0, "disabled": False, "errors": []})
    monkeypatch.setattr(jobs.follow, "run", lambda db_: calls.append("follow") or {"approved": 0})
    jobs.job_wb_poll(lambda p, c: None)
    assert calls == ["prices", "rule", "follow"]
