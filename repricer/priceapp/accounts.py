"""Кабинеты площадок: ключи (зашифрованные), клиент, загрузка каталога."""
from __future__ import annotations

from sqlalchemy.orm import Session

from priceapp import platforms
from priceapp.crypto import decrypt_value, encrypt_value
from priceapp.models import Account, ApiCredential, PlatformItem
from priceapp.timeutils import now_utc


def credentials(db: Session, account: Account) -> dict[str, str]:
    return {c.field_name: decrypt_value(c.encrypted_value)
            for c in db.query(ApiCredential).filter(ApiCredential.account_id == account.id)}


def set_credential(db: Session, account: Account, field: str, value: str) -> bool:
    """Записать ключ. Пустое значение НЕ стирает сохранённый: поле на странице
    показывается пустым (ключ не выводим), и сохранение формы без ввода иначе
    стирало бы все ключи кабинета. Возвращает True, если ключ изменён."""
    value = (value or "").strip()
    if not value:
        return False
    row = db.query(ApiCredential).filter(ApiCredential.account_id == account.id,
                                         ApiCredential.field_name == field).first()
    if row is None:
        row = ApiCredential(account_id=account.id, field_name=field)
        db.add(row)
    row.encrypted_value = encrypt_value(value)
    row.updated_at = now_utc()
    return True


def _dec_or_none(v):
    from decimal import Decimal
    return Decimal(str(v)).quantize(Decimal("0.01")) if v is not None else None


def client_for(db: Session, account: Account, factory=None):
    return (factory or platforms.build_client)(account.platform, credentials(db, account))


def load_catalog(db: Session, account: Account, client) -> dict:
    """Снимок каталога кабинета. Ключ строки — баркод (у WB одна карточка
    охватывает несколько размеров). Баркоды, пропавшие из выгрузки, удаляются —
    но ТОЛЬКО если выгрузка полная: огрызок стёр бы половину сопоставления."""
    rows = client.get_catalog()
    truncated = bool(getattr(client, "last_truncated", False))
    # Тарифы комиссий по категориям (у WB — отдельная таблица по предметам).
    # Не загрузились — каталог всё равно сохраняется, прежние тарифы остаются.
    tariffs, tariff_note = None, ""
    if hasattr(client, "get_tariffs"):
        try:
            tariffs = client.get_tariffs()
        except Exception as e:
            tariff_note = f"тарифы комиссий не загружены: {e}"[:300]
    existing = {i.barcode: i for i in db.query(PlatformItem).filter(PlatformItem.account_id == account.id)}
    seen = set()
    now = now_utc()
    for r in rows:
        if not r.barcode or r.barcode in seen:
            continue        # повтор баркода в выгрузке: второй INSERT уронил бы загрузку
        seen.add(r.barcode)
        item = existing.get(r.barcode)
        if item is None:
            item = PlatformItem(account_id=account.id, barcode=r.barcode[:64])
            db.add(item)
        item.external_id = (r.external_id or "")[:128]
        item.article = (r.article or "")[:200]
        item.name = (r.name or "")[:500]
        item.size = (r.size or "")[:64]
        item.category = (r.category or "")[:200]
        item.category_id = (r.category_id or "")[:64]
        if tariffs is not None:
            t = tariffs.get(item.category_id)
            item.tariff_fbs = _dec_or_none(t.fbs) if t else None
            item.tariff_fbo = _dec_or_none(t.fbo) if t else None
            item.tariff_loaded_at = now
        item.fetched_at = now
    removed = 0
    if not truncated:
        for bc, item in existing.items():
            if bc not in seen:
                db.delete(item)
                removed += 1
    account.catalog_loaded_at = now
    account.catalog_note = "; ".join(x for x in (
        "ВЫГРУЗКА НЕПОЛНАЯ: площадка не отдала каталог до конца, пропавшие строки не удалены"
        if truncated else "", tariff_note) if x)
    db.commit()
    return {"rows": len(seen), "removed": removed, "truncated": truncated}


def load_prices(db: Session, account: Account, client) -> dict:
    """Текущие цены площадки на строки каталога кабинета. Строки, о которых
    площадка не сказала, получают NULL — но ТОЛЬКО при полной выдаче: огрызок
    стёр бы цены у половины каталога и показал бы их «неизвестными»."""
    got = client.get_prices()
    truncated = bool(getattr(client, "last_truncated", False))
    mins, min_note = None, ""
    if hasattr(client, "get_min_prices"):
        try:
            mins = client.get_min_prices()
        except Exception as e:      # минимальные цены — предупреждение, цены важнее
            min_note = f"минимальные цены не загружены: {e}"[:300]
    now = now_utc()
    updated = missing = 0
    for item in db.query(PlatformItem).filter(PlatformItem.account_id == account.id):
        key = client.price_key(item)
        cur = got.get(key)
        if mins is not None:
            item.min_price = mins.get(key)
        if cur is not None:
            item.current_price, item.current_sale_price = cur.price, cur.sale_price
            # Тариф в ответе о ценах (Ozon) — свежее каталожного; WB его тут не даёт,
            # и его тариф из каталога затирать нельзя.
            if getattr(cur, "tariff_fbs", None) is not None or getattr(cur, "tariff_fbo", None) is not None:
                item.tariff_fbs, item.tariff_fbo = _dec_or_none(cur.tariff_fbs), _dec_or_none(cur.tariff_fbo)
                item.tariff_loaded_at = now
            item.price_status = getattr(cur, "status", "") or None
            item.price_loaded_at = now
            updated += 1
        else:
            missing += 1
            if not truncated:
                item.current_price = item.current_sale_price = item.price_status = None
                item.price_loaded_at = now
    account.prices_loaded_at = now
    account.prices_note = "; ".join(x for x in (
        "ВЫГРУЗКА ЦЕН НЕПОЛНАЯ: площадка не отдала её до конца, прежние цены у пропавших строк оставлены"
        if truncated else "", min_note) if x)
    db.commit()
    return {"updated": updated, "missing": missing, "truncated": truncated}
