"""Разовый помощник: печатает ID складов ПРОДАВЦА на площадках (WB/Ozon/Kit)
по каждому активному кабинету — их вписывают в поле «ID склада на площадке»
на странице «API-ключи».

Использует токены из базы приложения (расшифровывает через app.crypto).
Запуск из корня проекта:  python scripts\\list_warehouses.py
"""
import requests

from app.database import SessionLocal
from app.models import PlatformAccount, Platform
from app.workers.credentials import get_credentials, CredentialsMissing


def main():
    db = SessionLocal()
    accs = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all()
    if not accs:
        print("Нет активных кабинетов.")
        return

    for a in accs:
        print("")
        print("=== account #%s [%s] %s ===" % (a.id, a.platform.value, a.name))
        try:
            cr = get_credentials(db, a.id)
        except CredentialsMissing as e:
            print("  нет ключей:", e)
            continue
        try:
            if a.platform == Platform.wb:
                r = requests.get("https://marketplace-api.wildberries.ru/api/v3/warehouses",
                                 headers={"Authorization": cr["token"]}, timeout=30)
                r.raise_for_status()
                for w in r.json():
                    print("  WAREHOUSE id=%s  name=%s" % (w.get("id"), w.get("name")))
            elif a.platform == Platform.ozon:
                # /v1/warehouse/list устарел («obsolete method») — используем v2.
                r = requests.post("https://api-seller.ozon.ru/v2/warehouse/list",
                                  headers={"Client-Id": cr["client_id"], "Api-Key": cr["api_key"]},
                                  json={}, timeout=30)
                r.raise_for_status()
                d = r.json()
                rows = d.get("warehouses") or d.get("result") or []
                if not rows:
                    print("  raw:", r.text[:800])
                for w in rows:
                    print("  WAREHOUSE warehouse_id=%s  name=%s" % (w.get("warehouse_id"), w.get("name")))
            elif a.platform == Platform.kit:
                r = requests.get("https://api.kit.yandex.net/v1/warehouses",
                                 headers={"Authorization": "Bearer " + cr["token"]},
                                 params={"status": "ACTIVE"}, timeout=30)
                r.raise_for_status()
                data = r.json()
                whs = (data.get("warehouses") or data.get("result")
                       or (data if isinstance(data, list) else []))
                if not whs:
                    print("  raw:", data)
                for w in whs:
                    print("  WAREHOUSE id=%s  name=%s" % (w.get("id"), w.get("name")))
        except Exception as e:
            print("  ошибка запроса:", e)

    db.close()


if __name__ == "__main__":
    main()
