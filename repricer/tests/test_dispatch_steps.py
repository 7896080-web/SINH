"""Обычная отправка цены на WB: большое снижение — шагами (WB от 50% одним изменением не
применяет, больше порога карантина — в карантин)."""
from datetime import timedelta

from priceapp import dispatch, settings
from priceapp.models import PlatformItem, PriceChange, ProductPrice, ReferencePrice
from priceapp.timeutils import now_utc
from tests import factories as f


class _Push:
    def __init__(self):
        self.sent = []

    def push_prices(self, items):
        self.sent.append([(i.barcode, i.price) for i in items])
        return {"ok": [i.barcode for i in items], "errors": [], "sent_prices": {}}


def _setup(db, current=(10000, 5000), platform="wb"):
    f.manual_rate(db, "80")
    acc = f.account(db, platform=platform, commission=25)
    f.rule(db, acc, base_coef="2.5", min_markup_coef="1.01", max_change_percent=1000)
    f.sku(db, "u1", "A1", "S", barcodes=["b1"], cost_usd="1")
    f.item(db, acc, "b1", "A1", external_id="5:1", size="S")
    row = db.query(PlatformItem).filter_by(barcode="b1").one()
    row.current_price, row.current_sale_price = current
    db.add(PriceChange(item_id="u1", account_id=acc.id, barcode="b1", new_price=3000, status="approved", decided_by="op"))
    db.commit()
    return acc


def test_big_drop_goes_in_steps_until_target(db):
    acc = _setup(db)                                        # 10000 -> 3000: -70%
    push = _Push()
    st = dispatch.run_account(db, acc, push)
    assert push.sent == [[("b1", 6700)]] and st["stepped"] == 1          # не ниже 10000 x 0,67
    ch = db.query(PriceChange).one()
    assert ch.status == "approved" and "снижение шагами" in ch.note and ch.attempts == 0
    assert db.query(ProductPrice).count() == 0 and db.query(ReferencePrice).count() == 0   # эталон — по цели
    row = db.query(PlatformItem).one()
    assert (row.current_price, row.current_sale_price) == (6700, 3350)   # скидка 50% сохранена
    # до паузы следующий шаг не уходит
    assert dispatch.run_account(db, acc, push).get("waiting") == 1 and len(push.sent) == 1
    ch.next_attempt_at = now_utc() - timedelta(seconds=1)
    db.commit()
    dispatch.run_account(db, acc, push)
    assert push.sent[-1] == [("b1", 4489)]                               # 6700 x 0,67
    ch.next_attempt_at = now_utc() - timedelta(seconds=1)
    db.commit()
    dispatch.run_account(db, acc, push)
    assert push.sent[-1] == [("b1", 3008)]                               # 4489 x 0,67 = 3007,6 -> 3008
    ch.next_attempt_at = now_utc() - timedelta(seconds=1)
    db.commit()
    dispatch.run_account(db, acc, push)
    assert push.sent[-1] == [("b1", 3000)]                               # цель
    db.expire_all()
    assert db.query(PriceChange).one().status == "sent"
    assert db.query(ProductPrice).one().last_sent_price == 3000


def test_small_drop_and_raise_go_at_once(db):
    acc = _setup(db, current=(4000, 2000))                  # 4000 -> 3000: -25%
    push = _Push()
    dispatch.run_account(db, acc, push)
    assert push.sent == [[("b1", 3000)]] and db.query(PriceChange).one().status == "sent"


def test_step_size_is_a_setting_and_other_platforms_are_not_stepped(db):
    acc = _setup(db)
    settings.put(db, settings.WB_MAX_DROP_PERCENT, "45")
    db.commit()
    push = _Push()
    dispatch.run_account(db, acc, push)
    assert push.sent == [[("b1", 5500)]]


def test_ozon_is_not_stepped(db):
    acc = _setup(db, platform="ozon")
    push = _Push()
    dispatch.run_account(db, acc, push)
    assert push.sent == [[("b1", 3000)]]
