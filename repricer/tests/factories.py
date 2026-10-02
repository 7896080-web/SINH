from decimal import Decimal

from priceapp.models import Account, OnecBarcode, OnecCost, PlatformItem


def account(db, platform="wb", name="ИП Тест", commission=25):
    """Кабинет; комиссия ложится в правило ПЛОЩАДКИ (общее на её кабинеты)."""
    from priceapp.pricing import get_rule
    a = Account(platform=platform, name=name)
    db.add(a)
    get_rule(db, platform).commission_percent = Decimal(str(commission)) if commission is not None else None
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
    """Правило площадки кабинета `acc`: цена = базовая × 2,5 по умолчанию."""
    from priceapp.pricing import get_rule
    base = dict(base_coef="2.5", round_step=10, round_minus=1, min_markup_coef="1.3",
                max_change_percent=20)
    base.update(kw)
    r = get_rule(db, acc.platform)
    for k, v in base.items():
        setattr(r, k, Decimal(str(v)) if isinstance(v, str) else v)
    db.commit()
    return r


def manual_rate(db, value="81.5"):
    from priceapp import settings
    settings.put(db, settings.RATE_MODE, "manual")
    settings.put(db, settings.RATE_MANUAL, value)
    db.commit()


def coef(db, article, platform, value, account_id=0):
    """Коэффициент артикула (account_id 0 — на всю площадку)."""
    from priceapp.models import ArticleCoef
    db.add(ArticleCoef(article=article, platform=platform, account_id=account_id, coef=Decimal(str(value))))
    db.commit()
