from sqlalchemy.orm import Session

from app.models import Platform, PlatformAccount
from app.workers.credentials import get_credentials, CredentialsMissing
from app.workers.platform_clients.wb import WbClient
from app.workers.platform_clients.ozon import OzonClient
from app.workers.platform_clients.kit import KitClient


def build_client(db: Session, account_id: int):
    """Поднимает клиент конкретного кабинета по ключам, сохранённым в
    админке (раздел «API-ключи»). Поднимает CredentialsMissing, если
    чего-то не хватает, или ValueError, если кабинета не существует."""
    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        raise ValueError(f"Кабинет #{account_id} не найден")

    creds = get_credentials(db, account_id)

    if account.platform == Platform.wb:
        return WbClient(token=creds["token"], warehouse_id=account.warehouse_id or "")
    if account.platform == Platform.ozon:
        return OzonClient(client_id=creds["client_id"], api_key=creds["api_key"])
    if account.platform == Platform.kit:
        return KitClient(token=creds["token"])

    raise ValueError(account.platform)
