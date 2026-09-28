from sqlalchemy.orm import Session

from app.models import Platform, PlatformAccount, PlatformCatalogItem
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
        # Соответствие variant_id -> баркод у нас уже есть: снимок каталога
        # кабинета (`job_import_barcodes`, раз в 15 минут) кладёт идентификатор
        # варианта Kit в `external_id`. Отдаём клиенту карту из своей базы, чтобы
        # он не спрашивал площадку по одному варианту на строку заказа — именно
        # эти запросы упирались в 429, а 429 там оборачивался потерей заказа.
        # Ленивый загрузчик: запрос уйдёт, только если клиенту правда нужны
        # баркоды вариантов (при отправке остатков — не нужны).
        def load_variant_map() -> dict[str, str]:
            rows = db.query(
                PlatformCatalogItem.external_id, PlatformCatalogItem.barcode,
            ).filter(
                PlatformCatalogItem.account_id == account.id,
                PlatformCatalogItem.barcode.isnot(None),
            ).all()
            return {ext: bc for ext, bc in rows if ext}

        return KitClient(token=creds["token"], variant_map_loader=load_variant_map)

    raise ValueError(account.platform)
