from app.models import PlatformAccount, Platform


def make_account(db, platform: Platform = Platform.wb, name: str = "Тестовый кабинет",
                  warehouse_id: str = "wh-1", is_active: bool = True) -> PlatformAccount:
    account = PlatformAccount(platform=platform, name=name, warehouse_id=warehouse_id, is_active=is_active)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account
