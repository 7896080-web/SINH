from decimal import Decimal

from priceapp.models import Account, OnecBarcode, OnecCost, PlatformItem, PriceRule


def account(db, platform="wb", name="ИП Тест", commission=25):
    a = Account(platform=platform, name=name,
                commission_percent=Decimal(str(commission)) if commission is not None else None)
    db.add(a)
    db.commit()
    return a


def sku(db, item_id, article, size="", color="", barcodes=(), cost_usd=None, name="Товар"):
    for b in barcodes:
        db.add(OnecBarcode(barcode=b, item_id=item_id, article=article, name=name, size=size, color=color))
    if cost_usd is not None:
        db.add(OnecCost(item_id=item_id, cost_usd=Decimal(str(cost_usd))))
    db.commit()


def item(db, acc, barcode, article="", external_id="", size="", name="на площадке"):
    db.add(PlatformItem(account_id=acc.id, barcode=barcode, article=article,
                        external_id=external_id or f"x-{barcode}", size=size, name=name))
    db.commit()


def rule(db, acc, **kw):
    base = dict(markup_coef=2, round_step=10, round_minus=1, min_markup_coef="1.3",
                max_change_percent=20)
    base.update(kw)
    r = PriceRule(account_id=acc.id, **base)
    db.add(r)
    db.commit()
    return r


def manual_rate(db, value="81.5"):
    from priceapp import settings
    settings.put(db, settings.RATE_MODE, "manual")
    settings.put(db, settings.RATE_MANUAL, value)
    db.commit()
