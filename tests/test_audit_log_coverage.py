"""Находка 23 аудита — пробелы в журнале действий.

Смена и стирание токена площадки, смена склада кабинета и массовый импорт
баркодов не писались в журнал, хотя соседние операции пишутся. Заодно закрыт
вход в систему: она работает с боевыми токенами и остатками, и «кто и когда
заходил» — такая же часть аудита, как смена склада.

Отдельное требование ко ВСЕМ этим записям: в журнале не должно оказаться
секретов. Журнал открыт любому пользователю админки.
"""
import pytest

from app.models import ApiCredential, AuditLog, Platform, PlatformAccount


def _account(web_db, platform: Platform = Platform.wb, warehouse: str = "wh-1") -> PlatformAccount:
    account = PlatformAccount(platform=platform, name="ИП ЯВОРСКАЯ", warehouse_id=warehouse)
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    return account


def _entries(web_db, action: str) -> list[AuditLog]:
    return web_db.query(AuditLog).filter(AuditLog.action == action).all()


# ------------------------------------------------- токен площадки

def test_setting_a_token_is_written_to_audit(logged_in_client, web_db):
    account = _account(web_db)

    logged_in_client.post(f"/api-keys/accounts/{account.id}/token",
                          data={"value": "секретный-боевой-токен-123"})

    entries = _entries(web_db, "credential_changed")
    assert len(entries) == 1
    assert "ЯВОРСКАЯ" in entries[0].details


def test_token_value_never_gets_into_audit(logged_in_client, web_db):
    """Главное здесь: пишем ФАКТ, а не значение — журнал видит любой
    пользователь админки."""
    account = _account(web_db)

    logged_in_client.post(f"/api-keys/accounts/{account.id}/token",
                          data={"value": "секретный-боевой-токен-123"})

    for entry in web_db.query(AuditLog).all():
        assert "секретный-боевой-токен-123" not in (entry.details or "")


def test_clearing_a_token_is_written_to_audit(logged_in_client, web_db):
    """Стирание токена — отдельное действие: после него кабинет перестаёт
    работать, и причина должна быть видна в журнале."""
    account = _account(web_db)
    logged_in_client.post(f"/api-keys/accounts/{account.id}/token", data={"value": "токен"})

    logged_in_client.post(f"/api-keys/accounts/{account.id}/token", data={"value": ""})

    assert len(_entries(web_db, "credential_cleared")) == 1
    assert web_db.query(ApiCredential).first().encrypted_value is None


# ------------------------------------------------- склад кабинета

def test_warehouse_change_is_written_to_audit(logged_in_client, web_db):
    """Склад определяет, куда уходит остаток и какое перемещение создаётся
    в 1С."""
    account = _account(web_db, warehouse="wh-1")

    logged_in_client.post(f"/api-keys/accounts/{account.id}/warehouse",
                          data={"warehouse_id": "wh-2"})

    entries = _entries(web_db, "warehouse_changed")
    assert len(entries) == 1
    assert "wh-1" in entries[0].details and "wh-2" in entries[0].details


def test_unchanged_warehouse_does_not_spam_the_audit(logged_in_client, web_db):
    account = _account(web_db, warehouse="wh-1")

    logged_in_client.post(f"/api-keys/accounts/{account.id}/warehouse",
                          data={"warehouse_id": "wh-1"})

    assert _entries(web_db, "warehouse_changed") == []


# ------------------------------------------------- массовый импорт баркодов

def test_mapping_import_is_written_to_audit(logged_in_client, web_db):
    import io

    from openpyxl import Workbook

    from app.models import Product

    web_db.add(Product(uid_1c="u1", article="A", name="Т", stock_on_hand=1))
    web_db.commit()

    wb = Workbook()
    ws = wb.active
    ws.append(["ID_1С", "Баркод"])
    ws.append(["u1", "111"])
    buffer = io.BytesIO()
    wb.save(buffer)

    logged_in_client.post("/mapping/import",
                          files={"file": ("import.xlsx", buffer.getvalue(),
                                          "application/vnd.openxmlformats-officedocument."
                                          "spreadsheetml.sheet")})

    entries = _entries(web_db, "mapping_import")
    assert len(entries) == 1
    assert "добавлено 1" in entries[0].details


# ------------------------------------------------- вход в систему

def _user(web_db, username: str = "operator"):
    from app.models import User
    from app.security import hash_password

    web_db.add(User(username=username, password_hash=hash_password("secret123")))
    web_db.commit()
    return username


def test_successful_login_is_written_to_audit(client, web_db):
    username = _user(web_db)

    client.post("/login", data={"username": username, "password": "secret123"})

    entries = _entries(web_db, "login_ok")
    assert len(entries) == 1
    assert entries[0].actor == username


def test_failed_login_is_written_to_audit(client, web_db):
    username = _user(web_db)

    client.post("/login", data={"username": username, "password": "неверный"})

    assert len(_entries(web_db, "login_failed")) == 1


def test_lockout_is_written_to_audit(client, web_db):
    """Блокировка после серии неудач — отдельное событие: по журналу должно быть
    видно, что аккаунт закрыли не руками."""
    from app.login_security import MAX_FAILED_ATTEMPTS

    username = _user(web_db)

    for _ in range(MAX_FAILED_ATTEMPTS):
        client.post("/login", data={"username": username, "password": "неверный"})

    assert len(_entries(web_db, "login_locked")) == 1


def test_password_never_gets_into_audit(client, web_db):
    username = _user(web_db)

    client.post("/login", data={"username": username, "password": "неверный-пароль-123"})

    for entry in web_db.query(AuditLog).all():
        assert "неверный-пароль-123" not in (entry.details or "")
