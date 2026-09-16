from datetime import datetime
from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.models import Barcode, SyncSetting, ProposalSource, PlatformCatalogItem, PlatformAccount


def poll_catalog(db: Session, account: PlatformAccount) -> dict:
    """Раз в сутки: карточка уже существует в кабинете, баркод однозначно
    сопоставлен, но синхронизация не включена именно для этого кабинета —
    ставим мягкий индикатор-предложение, НЕ включаем сами (раздел 10
    спецификации). Работает от уже загруженного снимка PlatformCatalogItem —
    сам поход в API делает load_platform_catalog() (catalog_sync.py)."""

    catalog_barcodes = {
        row.barcode for row in
        db.query(PlatformCatalogItem.barcode).filter(PlatformCatalogItem.account_id == account.id).all()
    }
    stats = {"proposed": 0, "already_enabled": 0, "already_proposed": 0}

    if not catalog_barcodes:
        return stats

    # Дедупликация по товару: у одного uid_1c может быть несколько баркодов
    # (размеры) — если совпало 2+, нельзя плодить дубли SyncSetting (пара
    # товар+кабинет уникальна), иначе IntegrityError на commit.
    matched_uids = {
        row.uid_1c for row in
        db.query(Barcode.uid_1c).filter(Barcode.barcode.in_(catalog_barcodes)).all()
    }

    for uid_1c in matched_uids:
        setting = db.query(SyncSetting).filter(
            SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account.id,
        ).first()

        if setting is None:
            setting = SyncSetting(uid_1c=uid_1c, account_id=account.id, enabled=False)
            db.add(setting)

        if setting.enabled:
            stats["already_enabled"] += 1
            continue

        if setting.has_proposal:
            stats["already_proposed"] += 1
            continue

        setting.has_proposal = True
        setting.proposal_source = ProposalSource.catalog_detected
        setting.proposal_date = now_utc()
        stats["proposed"] += 1

    db.commit()
    return stats
