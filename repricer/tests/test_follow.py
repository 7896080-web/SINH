"""Kit следует за WB: цена Kit = 90% средней цены WB для покупателя; пересчёт маржинальности, автоподтверждение
до шага 50%, ручное — крупнее и для первой партии; триггеры опроса после отправки цен на WB."""
from datetime import timedelta
from decimal import Decimal

from priceapp import follow, settings
from priceapp.models import ArticleCoef, CardEcon, PlatformItem, PriceChange, ProductPrice
from priceapp.timeutils import now_utc
from tests import factories as f


def _setup(db, wb_sale=5000, kit_price=3000, sent=True):
    """Артикул A1: себестоимость 10 $ × 80 = 800 ₽; на WB цена продавца wb_sale при доле W/P 0,6 → покупателю
    wb_sale × 0,6; на Kit — kit_price. Комиссия Kit 40%."""
    f.manual_rate(db, "80")
    wb = f.account(db, platform="wb", name="ИП WB", commission=25)
    f.rule(db, wb, base_coef=None, max_change_percent=1000)
    kit = f.account(db, platform="kit", name="КИТ", commission=40)
    f.rule(db, kit, base_coef=None, min_markup_coef="1.3", max_change_percent=1000, round_step=1, round_minus=0)
    f.sku(db, "u1", "A1", "S", barcodes=["bw", "bk"], cost_usd="10")
    f.item(db, wb, "bw", "A1", external_id="7:1", size="S")
    f.item(db, kit, "bk", "A1", external_id="k-1", size="S")
    for row in db.query(PlatformItem):
        row.current_price = row.current_sale_price = wb_sale if row.account_id == wb.id else kit_price
    db.add(CardEcon(account_id=wb.id, card="7", article="A1", net_units=5, k=0.7, w=0.6, f_logistics=0, how="тест"))
    if sent:
        db.add(ProductPrice(item_id="u1", account_id=kit.id, last_sent_price=kit_price))
    db.commit()
    return wb, kit


def _margin(db, kit):
    c = db.query(ArticleCoef).filter_by(account_id=kit.id, article="A1").first()
    return c.coef if c else None


def test_target_price_is_90_percent_of_average_wb_buyer_price(db):
    wb, kit = _setup(db)
    assert round(follow.wb_average(db)["A1"]) == 3000                      # 5000 × 0,6
    st = follow.run(db)
    # цена Kit = 0,9 × 3000 = 2700; маржинальность = 2700 × 0,6 / 800 = 2,025
    assert st["margins"] == 1 and _margin(db, kit) == Decimal("2.025")
    ch = db.query(PriceChange).filter_by(account_id=kit.id, status="approved").one()
    assert ch.new_price == 2700 and ch.source == "follow" and ch.decided_by == follow.ACTOR
    assert st["approved"] == 1


def test_first_batch_is_left_for_manual_approval(db):
    wb, kit = _setup(db, sent=False)
    st = follow.run(db)
    assert st["approved"] == 0 and st["manual"] == 1
    assert db.query(PriceChange).filter_by(account_id=kit.id, status="proposed").count() == 1


def test_step_over_50_percent_stays_manual(db):
    wb, kit = _setup(db, kit_price=1500)                                    # 2700 / 1500 = +80%
    st = follow.run(db)
    assert st["approved"] == 0 and st["manual"] == 1
    ch = db.query(PriceChange).filter_by(account_id=kit.id, status="proposed").one()
    assert "подтвердите вручную" in ch.note


def test_follows_wb_price_change_and_ignores_small_moves(db):
    wb, kit = _setup(db)
    follow.run(db)
    db.query(PriceChange).filter_by(account_id=kit.id).update({"status": "sent"})
    db.query(ProductPrice).filter_by(account_id=kit.id).update({"last_sent_price": 2700})
    for row in db.query(PlatformItem).filter_by(account_id=kit.id):
        row.current_price = row.current_sale_price = 2700
    db.commit()
    assert follow.run(db)["approved"] == 0                                  # ничего не менялось
    for row in db.query(PlatformItem).filter_by(account_id=wb.id):          # WB поднял цену на 0,4% — в пределах 1%
        row.current_price = row.current_sale_price = 5020
    db.commit()
    assert follow.run(db)["approved"] == 0
    for row in db.query(PlatformItem).filter_by(account_id=wb.id):          # цена WB +20% → Kit тоже
        row.current_price = row.current_sale_price = 6000
    db.commit()
    st = follow.run(db)
    assert st["approved"] == 1
    assert db.query(PriceChange).filter_by(account_id=kit.id, status="approved").one().new_price == 3240


def test_kit_price_changed_by_platform_is_returned(db):
    wb, kit = _setup(db)
    follow.run(db)
    db.query(PriceChange).filter_by(account_id=kit.id).update({"status": "sent"})
    db.query(ProductPrice).filter_by(account_id=kit.id).update({"last_sent_price": 2700})
    for row in db.query(PlatformItem).filter_by(account_id=kit.id):
        row.current_price = row.current_sale_price = 2200                    # площадка сама снизила цену
    db.commit()
    st = follow.run(db)
    assert st["recalculated"] and st["approved"] == 1
    assert db.query(PriceChange).filter_by(account_id=kit.id, status="approved").one().new_price == 2700


def test_disabled_does_nothing(db):
    wb, kit = _setup(db)
    settings.put(db, settings.FOLLOW_ENABLED, "0")
    db.commit()
    assert follow.run(db)["disabled"] and db.query(ArticleCoef).count() == 0


def test_dispatch_to_wb_schedules_follow_poll(db):
    from priceapp.workers import jobs
    wb, kit = _setup(db)
    settings.put(db, settings.FOLLOW_DUE_AT, (now_utc() + timedelta(minutes=5)).isoformat())
    db.commit()
    assert jobs.wb_poll_requested() is False                                # срок не наступил
    settings.put(db, settings.FOLLOW_DUE_AT, (now_utc() - timedelta(seconds=1)).isoformat())
    db.commit()
    assert jobs.wb_poll_requested() is True and jobs.wb_poll_requested() is False   # флаг снимается
