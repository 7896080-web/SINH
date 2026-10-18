"""Кабинеты площадок и их ключи (зашифрованные своим ключом программы)."""
from __future__ import annotations

from cryptography.fernet import InvalidToken
from sqlalchemy.orm import Session

from finapp.crypto import decrypt_value, encrypt_value
from finapp.models import ApiCredential, PlatformAccount
from finapp.timeutils import now_utc

# Какие ключи нужны кабинету. WB: токен категории «Финансы» (finance-api) —
# в кабинете WB «Настройки → Доступ к API», «Только на чтение» достаточно. KIT:
# токен магазина (тот же, что у sync_admin, но вписывается здесь отдельно — у
# программ разные ключи шифрования). Lamoda и Ozon — отчёты файлом, ключей нет.
CREDENTIAL_FIELDS = {
    "wb": [("finance_token", "Токен WB с категорией «Финансы»")],
    "kit": [("token", "Токен Яндекс KIT")],
    "lamoda": [],
    "ozon": [],
}

UNREADABLE = "не расшифрован — впишите ключ заново"


def credentials(db: Session, account: PlatformAccount, strict: bool = False) -> dict[str, str]:
    """Ключи кабинета. Сменился `MPFIN_SECRETS_KEY` — старые ключи не расшифровать:
    `strict` даёт понятную ошибку с именем кабинета, иначе вместо значения пустая
    строка (страница «API-ключи» показывает пометку и даёт вписать заново)."""
    out = {}
    for row in db.query(ApiCredential).filter(ApiCredential.account_id == account.id):
        try:
            out[row.field_name] = decrypt_value(row.encrypted_value)
        except InvalidToken:
            if strict:
                raise RuntimeError(f"{account.name}: ключ «{row.field_name}» {UNREADABLE} на "
                                   f"странице «API-ключи» (сменился MPFIN_SECRETS_KEY)") from None
            out[row.field_name] = ""
    return out


def unreadable_fields(db: Session, account: PlatformAccount) -> set[str]:
    bad = set()
    for row in db.query(ApiCredential).filter(ApiCredential.account_id == account.id):
        try:
            decrypt_value(row.encrypted_value)
        except InvalidToken:
            bad.add(row.field_name)
    return bad


def set_credential(db: Session, account: PlatformAccount, field: str, value: str) -> bool:
    """Записать ключ. Пустое значение НЕ стирает сохранённый: поле на странице
    показывается пустым (ключ не выводим), и сохранение формы без ввода иначе
    стирало бы все ключи кабинета. True — ключ изменён."""
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
