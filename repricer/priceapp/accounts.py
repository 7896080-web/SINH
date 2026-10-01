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


def client_for(db: Session, account: Account, factory=None):
    return (factory or platforms.build_client)(account.platform, credentials(db, account))


def load_catalog(db: Session, account: Account, client) -> dict:
    """Снимок каталога кабинета. Ключ строки — баркод (у WB одна карточка
    охватывает несколько размеров). Баркоды, пропавшие из выгрузки, удаляются —
    но ТОЛЬКО если выгрузка полная: огрызок стёр бы половину сопоставления."""
    rows = client.get_catalog()
    truncated = bool(getattr(client, "last_truncated", False))
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
        item.fetched_at = now
    removed = 0
    if not truncated:
        for bc, item in existing.items():
            if bc not in seen:
                db.delete(item)
                removed += 1
    account.catalog_loaded_at = now
    account.catalog_note = ("ВЫГРУЗКА НЕПОЛНАЯ: площадка не отдала каталог до конца, "
                            "пропавшие строки не удалены" if truncated else "")
    db.commit()
    return {"rows": len(seen), "removed": removed, "truncated": truncated}
