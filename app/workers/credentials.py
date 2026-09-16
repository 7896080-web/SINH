from sqlalchemy.orm import Session

from app.models import ApiCredential, PlatformAccount, Platform
from app.crypto import decrypt_value

# Обязательные поля по ТИПУ площадки — одинаковы для всех кабинетов этой
# площадки (у любого кабинета WB нужен token, у любого Ozon — client_id+api_key).
REQUIRED_FIELDS = {
    Platform.wb: ["token"],
    Platform.ozon: ["client_id", "api_key"],
    Platform.kit: ["token"],
}


class CredentialsMissing(Exception):
    """Поднимается, если для кабинета не заполнен хотя бы один обязательный ключ."""


def get_credentials(db: Session, account_id: int) -> dict:
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        raise CredentialsMissing(f"Кабинет #{account_id} не найден")

    rows = db.query(ApiCredential).filter(ApiCredential.account_id == account_id).all()
    result = {row.field_name: (decrypt_value(row.encrypted_value) if row.encrypted_value else "") for row in rows}

    required = REQUIRED_FIELDS.get(account.platform, [])
    missing = [f for f in required if not result.get(f)]
    if missing:
        raise CredentialsMissing(
            f"Для кабинета «{account.name}» ({account.platform.value}) не заполнены ключи: {', '.join(missing)}"
        )

    return result
