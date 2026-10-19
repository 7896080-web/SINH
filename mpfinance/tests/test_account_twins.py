"""Два кабинета с одним названием (`routers/accounts.py`).

19.10.2026 на бою у KIT было два кабинета «Яндекс» при одном магазине.
Автозагрузка продаж из AWER CRM из двух кабинетов не угадывает
(`fetch.crm_account`) — и продажи KIT за октябрь не пришли вовсе. Второй такой
кабинет теперь не завести, а лишний пустой — удалить кнопкой."""
from finapp import mail, settings
from finapp.finance import fetch
from finapp.models import ApiCredential, AuditLog, PlatformAccount, ReportFetch
from tests.finance_factories import make_account, make_report
from tests.test_finance_api_fetch import _cred


def test_second_cabinet_with_the_same_name_is_refused(client, db):
    make_account(db, platform="kit", name="Яндекс")
    page = client.post("/api-keys/new", data={"platform": "kit", "name": " яндекс "}, follow_redirects=True).text
    assert "уже есть — второй не заведён" in page
    assert db.query(PlatformAccount).filter(PlatformAccount.platform == "kit").count() == 1
    client.post("/api-keys/new", data={"platform": "wb", "name": "Яндекс"})         # другая площадка — можно
    assert db.query(PlatformAccount).count() == 2


def test_rename_into_a_twin_is_refused(client, db):
    make_account(db, platform="kit", name="Яндекс")
    other = make_account(db, platform="kit", name="Сайт")
    page = client.post(f"/api-keys/{other.id}", data={"name": "Яндекс", "is_active": "1"},
                       follow_redirects=True).text
    assert "уже есть — ничего не сохранено" in page
    db.refresh(other)
    assert other.name == "Сайт" and other.is_active


def test_empty_twin_is_deleted_and_crm_autoload_finds_its_cabinet(client, db, crm_db):
    kept = make_account(db, platform="kit", name="Яндекс")
    twin = make_account(db, platform="kit", name="Яндекс")             # как на бою: тот же
    make_report(db, platform="kit", account_id=kept.id, lines=[{"revenue": 100000, "payout": 100000}])
    _cred(db, twin, "token", "t")
    db.add(ReportFetch(kind="kit_api", account_id=twin.id, status="failed"))
    settings.put(db, mail.ACCOUNT, str(twin.id))
    db.commit()
    assert fetch.crm_account(db) is None                              # из двух не угадывает

    page = client.get("/api-keys").text
    assert f'action="/api-keys/{twin.id}/delete"' in page
    assert f'action="/api-keys/{kept.id}/delete"' not in page          # с отчётами — кнопки нет

    twin_id = twin.id
    page = client.post(f"/api-keys/{twin_id}/delete", follow_redirects=True).text
    assert "Кабинет «Яндекс» удалён" in page
    db.expire_all()
    assert db.get(PlatformAccount, twin_id) is None
    assert db.query(ApiCredential).filter(ApiCredential.account_id == twin_id).count() == 0
    assert db.query(ReportFetch).filter(ReportFetch.account_id == twin_id).count() == 0
    assert settings.get(db, mail.ACCOUNT) == ""
    assert db.query(AuditLog).filter(AuditLog.action == "account_deleted").count() == 1
    assert fetch.crm_account(db).id == kept.id


def test_cabinet_with_reports_is_not_deleted(client, db):
    account = make_account(db, platform="kit", name="Яндекс")
    make_report(db, platform="kit", account_id=account.id, lines=[{"revenue": 100000, "payout": 100000}])
    page = client.post(f"/api-keys/{account.id}/delete", follow_redirects=True).text
    assert "не удалён: на нём отчётов 1" in page
    db.expire_all()
    assert db.get(PlatformAccount, account.id) is not None
