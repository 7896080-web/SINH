import pytest

from app.models import ApiCredential, Platform
from app.crypto import encrypt_value
from app.workers.credentials import get_credentials, CredentialsMissing
from tests.factories import make_account


def test_missing_when_account_not_found(db):
    with pytest.raises(CredentialsMissing):
        get_credentials(db, 99999)


def test_missing_when_no_rows_at_all(db):
    """Краевой случай: страницу API-ключей ещё ни разу не открывали —
    в таблице вообще нет строк под кабинет."""
    account = make_account(db, platform=Platform.ozon)
    with pytest.raises(CredentialsMissing):
        get_credentials(db, account.id)


def test_missing_when_field_empty(db):
    account = make_account(db, platform=Platform.wb)
    db.add(ApiCredential(account_id=account.id, field_name="token", field_label="Токен", encrypted_value=None))
    db.commit()

    with pytest.raises(CredentialsMissing):
        get_credentials(db, account.id)


def test_returns_all_required_fields_when_filled(db):
    account = make_account(db, platform=Platform.ozon)
    db.add(ApiCredential(account_id=account.id, field_name="client_id",
                          field_label="Client-Id", encrypted_value=encrypt_value("123")))
    db.add(ApiCredential(account_id=account.id, field_name="api_key",
                          field_label="Api-Key", encrypted_value=encrypt_value("secret")))
    db.commit()

    creds = get_credentials(db, account.id)
    assert creds == {"client_id": "123", "api_key": "secret"}


def test_two_wb_cabinets_have_independent_credentials(db):
    """Ключевой сценарий: у трёх кабинетов WB — три разных токена, не один
    общий на всю площадку."""
    wb1 = make_account(db, platform=Platform.wb, name="ИП Яворская")
    wb2 = make_account(db, platform=Platform.wb, name="ИП Ребрик")

    db.add(ApiCredential(account_id=wb1.id, field_name="token", field_label="Токен",
                          encrypted_value=encrypt_value("token-yavorskaya")))
    db.add(ApiCredential(account_id=wb2.id, field_name="token", field_label="Токен",
                          encrypted_value=encrypt_value("token-rebrik")))
    db.commit()

    creds1 = get_credentials(db, wb1.id)
    creds2 = get_credentials(db, wb2.id)

    assert creds1["token"] == "token-yavorskaya"
    assert creds2["token"] == "token-rebrik"
