"""«Полный список — по ссылке ниже» обязан вести к полному списку.

21.09 на бою: находка «Площадка не знает наш sku: 51 позиций» показывала десять
строк, писала «…и ещё 41. Полный список — по ссылке ниже», а ссылка «Разобрать →»
открывала общий каталог товаров. Обещание было, списка не было — оператор
справедливо спросил, где ему это смотреть.

Там же вскрылось второе: причина отказа обрывалась на полуслове —

    … · КИТ — [{'detail': '400 Client Error: Bad Request for url:
    https://api.kit.yandex.net/v1/variants

Ответ площадки рассылка сохраняет целиком; съедала его обвязка, а обрезание шло
по 90 символам с начала строки. Разобрать по такому нельзя ничего.
"""

from app.models import (Barcode, DispatchQueueItem, DispatchStatus, Platform,
                        Product, SyncSetting)
from app.report import FULL_LISTS, _error_gist, collect_findings
from tests.factories import make_account

KIT_ERROR = ("не отправлено за 5 попыток: [{'detail': '400 Client Error: Bad Request "
             "for url: https://api.kit.yandex.net/v1/variants/stocks/bulk_update: "
             '{"code": "VALIDATION_ERROR", "message": "variant not found"}\'}]')


def _failed(db, account, uid="u1", error=KIT_ERROR):
    db.add(Product(uid_1c=uid, article="2403", name="Конко Джемпер",
                   size="50/50", color="A.INDIGOMEL", stock_on_hand=7,
                   broadcast_enabled=True))
    db.add(Barcode(barcode=f"bc-{uid}", uid_1c=uid))
    # Кабинет ОТМЕЧЕН. Без этого пара «мёртвая» — по неотмеченному кабинету,
    # на который мы ни разу не отправляли, отказ расхождением не считается
    # (см. `report._only_live_pairs`), и проверять тут было бы нечего.
    db.add(SyncSetting(uid_1c=uid, account_id=account.id, enabled=True))
    db.add(DispatchQueueItem(uid_1c=uid, account_id=account.id, quantity=7,
                             status=DispatchStatus.error, reason="test",
                             is_test=False, last_error=error, sent_sku="v-1"))
    db.commit()


# ----------------------------------------------------- причина стала читаемой

def test_the_platform_answer_survives():
    """Главное: то, что ответила площадка, обязано остаться в строке."""
    assert "VALIDATION_ERROR" in _error_gist(KIT_ERROR)


def test_the_boilerplate_is_dropped():
    gist = _error_gist(KIT_ERROR)

    assert "Client Error" not in gist
    assert "попыток" not in gist
    assert "api.kit.yandex.net" not in gist


def test_the_status_code_is_kept():
    """400 и 409 у площадок значат разное — потерять код нельзя."""
    assert _error_gist(KIT_ERROR).startswith("400")


def test_an_answer_without_a_body_says_so():
    text = ("попытка 2 из 5: [{'detail': '409 Client Error: Conflict for url: "
            "https://suppliers-api.wb.ru/api/v3/stocks/123'}]")

    assert "409" in _error_gist(text)


def test_our_own_message_is_left_alone(db):
    """Не всякая ошибка приходит от площадки — свои объяснения не трогаем."""
    own = "нет карточки в каталоге кабинета — остаток отправить не по чему"

    assert _error_gist(own) == own


def test_an_empty_error_is_not_an_error():
    assert _error_gist(None) == ""
    assert _error_gist("") == ""


def test_the_finding_shows_the_readable_reason(db):
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _failed(db, account)

    finding = [f for f in collect_findings(db) if f.key == "dispatch_errors"][0]

    assert "VALIDATION_ERROR" in finding.details[0]


# ------------------------------------------------------- страница со списком

def test_every_row_based_finding_has_a_full_list(db):
    """Находка, обещающая полный список, обязана его иметь. Ссылка на страницу
    без списка — это и есть то, на что наступил оператор."""
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _failed(db, account)

    for finding in collect_findings(db):
        if finding.link.startswith("/report/rows/"):
            assert finding.link.rsplit("/", 1)[1] in FULL_LISTS, finding.key


def test_the_full_list_returns_every_row(db):
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    for i in range(15):
        _failed(db, account, uid=f"u{i}")

    _, _, fn = FULL_LISTS["dispatch_errors"]

    assert len(fn(db)) == 15, "список обязан быть полным, а не первой десяткой"


def test_the_row_names_the_product_as_people_do(db):
    account = make_account(db, Platform.kit, name="КИТ", warehouse_id="wh-2")
    _failed(db, account)

    _, columns, fn = FULL_LISTS["dispatch_errors"]
    row = fn(db)[0]

    assert "Артикул" in columns
    assert "2403" in row
    assert "КИТ" in row
    assert any("VALIDATION_ERROR" in str(cell) for cell in row)


def test_the_page_opens_and_lists(logged_in_client, web_db):
    from app.models import PlatformAccount
    account = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-2")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    _failed(web_db, account)

    page = logged_in_client.get("/report/rows/dispatch_errors")

    assert page.status_code == 200
    assert "2403" in page.text
    assert "VALIDATION_ERROR" in page.text


def test_the_page_offers_the_export(logged_in_client, web_db):
    from app.models import PlatformAccount
    account = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-2")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    _failed(web_db, account)

    page = logged_in_client.get("/report/rows/dispatch_errors").text

    assert "/report/rows/dispatch_errors/export" in page


def test_the_export_is_a_spreadsheet(logged_in_client, web_db):
    from app.models import PlatformAccount
    account = PlatformAccount(platform=Platform.kit, name="КИТ", warehouse_id="wh-2")
    web_db.add(account)
    web_db.commit()
    web_db.refresh(account)
    _failed(web_db, account)

    r = logged_in_client.get("/report/rows/dispatch_errors/export")

    assert r.status_code == 200
    assert r.content[:2] == b"PK", "должен быть .xlsx"


def test_an_unknown_key_does_not_crash(logged_in_client):
    """Ссылку могут открыть из старой вкладки или из лога."""
    r = logged_in_client.get("/report/rows/нетакой")

    assert r.status_code == 404
    assert "Находка не найдена" in r.text


def test_an_empty_list_says_it_is_resolved(logged_in_client, web_db):
    """Отчёт обновляется сам: к моменту открытия строк может уже не быть."""
    page = logged_in_client.get("/report/rows/negative_stock").text

    assert "разобрана" in page
