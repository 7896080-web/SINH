"""Сверка со сводом WB и проверка доступа к API.

Свод в тестах собран в раскладке настоящей выгрузки «Финансовые отчёты» из
кабинета WB (271 отчёт за 29.01.2024–11.10.2026, заголовки слово в слово)."""
from datetime import date

import pytest

from finapp.crypto import encrypt_value
from finapp.finance import fetch, statements
from finapp.finance import values as v
from finapp.finance.importer import import_report
from finapp.finance.sources import wb as wb_source
from finapp.models import ApiCredential, PlatformStatement, SalesReport
from tests.finance_factories import make_account, make_product, make_report, make_xlsx, wb_file, wb_row
from tests.test_finance_api_fetch import WB_HEADER, FakeResponse, FakeSession, _wb_line

SUMMARY_HEADERS = [
    "№ отчета", "Юридическое лицо", "Дата начала", "Дата конца", "Дата формирования", "Тип отчета",
    "Продажа", "В том числе Компенсация скидки по программе лояльности", "К перечислению за товар",
    "Согласованная скидка, %", "Стоимость доставки", "Стоимость хранения", "Стоимость обработки товара",
    "Прочие удержания/выплаты", "Общая сумма штрафов", "Корректировка Вознаграждения Вайлдберриз (ВВ)",
    "Стоимость участия в программе лояльности", "Сумма баллов, удержанных по программе лояльности",
    "Разовое изменение срока перечисления денежных средств", "Итого к оплате", "Валюта"]


def _summary_row(number, start, end, kind, for_pay, delivery, storage, acceptance, other, penalty, total,
                 entity="ИП Яворская Т. Н."):
    return [number, entity, f"{start}T00:00:00+03:00", f"{end}T00:00:00+03:00",
            "2026-09-20T21:48:03+00:00", kind, 0, 0, for_pay, 0, delivery, storage, acceptance, other,
            penalty, 0, 0, 0, 0, total, "руб."]


# Строки — из настоящего свода (14–20.09.2026 и первый отчёт свода).
REAL_ROWS = [
    _summary_row("848253942", "2026-09-14", "2026-09-20", "Основной", 477893.76, 121549.61, 7699.06,
                 210, 13538, 0, 334897.09),
    _summary_row("852719850", "2026-09-14", "2026-09-20", "По выкупам", 7650.15, 471.29, 0, 0, 0, 0,
                 7178.86),
    _summary_row("250613472", "2024-01-29", "2024-02-04", "Основной", 1873694.49, 120219.73, 36969.04,
                 0, 87337.15, 3112, 1626056.57),
]


def _summary_file(rows=REAL_ROWS):
    return make_xlsx(SUMMARY_HEADERS, rows)


def _parse(data):
    from finapp.finance.readers import read_sheets
    return statements.parse_wb_summary(read_sheets(data, "svod.xlsx"))


def test_summary_is_parsed_and_the_wb_formula_holds():
    rows = _parse(_summary_file())
    assert [r["report_number"] for r in rows] == ["848253942", "852719850", "250613472"]
    first = rows[0]
    assert (first["period_from"], first["period_to"], first["report_kind"]) == \
        (date(2026, 9, 14), date(2026, 9, 20), "основной")
    assert rows[1]["report_kind"] == "по выкупам"
    for r in rows:
        assert r["for_pay_kop"] - r["delivery_kop"] - r["storage_kop"] - r["acceptance_kop"] \
            - r["deduction_kop"] - r["penalty_kop"] == r["total_kop"]


def test_not_a_summary_is_refused():
    with pytest.raises(statements.StatementError, match="Итого к оплате"):
        _parse(make_xlsx(["Что-то"], [["1"]]))


def test_broken_row_refuses_the_whole_summary():
    bad = list(REAL_ROWS[0])
    bad[19] = "много"
    with pytest.raises(statements.StatementError, match="строка 2"):
        _parse(_summary_file([bad]))


def _wb_report(db, number, lines):
    report = make_report(db, platform="wb", period=(date(2026, 9, 14), date(2026, 9, 20)),
                         key=f"SR-wb-{number}", lines=lines)
    report.report_number = number
    db.commit()
    return report


def test_compare_finds_missing_matching_and_diverging_reports(db):
    statements.store(db, "wb", None, _parse(_summary_file()), "file")
    db.commit()
    # Отчёт 852719850 сходится до копейки, 848253942 — доставка у нас другая.
    _wb_report(db, "852719850", [{"operation": v.SALE, "quantity": 1, "payout": 765015},
                                 {"operation": v.LOGISTICS, "quantity": 0, "logistics": 47129}])
    _wb_report(db, "848253942", [{"operation": v.SALE, "quantity": 1, "payout": 47789376},
                                 {"operation": v.LOGISTICS, "quantity": 0, "logistics": 12000000},
                                 {"operation": v.STORAGE, "quantity": 0, "storage": 769906},
                                 {"operation": v.ACCEPTANCE, "quantity": 0, "acceptance": 21000},
                                 {"operation": v.DEDUCTION, "quantity": 0, "deduction": 1353800}])
    rows = {r.statement.report_number: r for r in statements.compare(db)}
    assert rows["250613472"].status == "missing"
    assert rows["852719850"].status == "ok"
    diverging = rows["848253942"]
    assert diverging.status == "diff"
    assert diverging.diff == 12154961 - 12000000
    assert diverging.bucket_diffs() == [("Доставка", 12154961, 12000000)]


# Два ИП — два кабинета WB со своими сводами. Номера отчётов у WB сквозные, и
# 848966494 (ИП Яворский) в своде ИП Яворской отсутствует по праву.
OTHER = "ИП Яворский"
OTHER_ROWS = [_summary_row("848966494", "2026-09-14", "2026-09-20", "Основной", 60000, 12000, 500, 0, 264.02,
                           0, 47235.98, entity=OTHER)]


def _two_cabinets(db):
    her = make_account(db, name="WB Яворская")
    his = make_account(db, name="WB Яворский")
    statements.store(db, "wb", her.id, _parse(_summary_file()), "file")
    db.commit()
    return her, his


def test_compare_is_filtered_by_cabinet(db):
    her, his = _two_cabinets(db)
    statements.store(db, "wb", his.id, _parse(_summary_file(OTHER_ROWS)), "file")
    db.commit()
    numbers = lambda account: {r.statement.report_number for r in statements.compare(db, account=account)}
    assert numbers(her.id) == {"848253942", "852719850", "250613472"}
    assert numbers(his.id) == {"848966494"}
    assert numbers(None) == numbers(her.id) | numbers(his.id)
    assert numbers(statements.NO_ACCOUNT) == set()
    row = statements.compare(db, account=his.id)[0]
    assert row.statement.legal_entity == OTHER and row.statement.total_kop == 4723598


def test_loaded_report_absent_from_every_summary_is_listed(db):
    her, his = _two_cabinets(db)
    report = _wb_report(db, "848966494", [{"operation": v.SALE, "quantity": 1, "payout": 4723598}])
    report.account_id = his.id
    db.commit()
    extra = lambda account: [r.report_number for r, _ in
                             statements.not_in_summary(db, "wb", statements.compare(db, account=account),
                                                       account)]
    assert extra(None) == ["848966494"]
    assert statements.not_in_summary(db, "wb", statements.compare(db))[0][1] == 4723598
    assert extra(her.id) == []                       # отчёт чужого кабинета её своду не в упрёк
    # Свод самого кабинета ещё не загружен — показать не по чему (за пределами
    # свода отсутствие ничего не значит).
    assert extra(his.id) == []
    statements.store(db, "wb", his.id, _parse(_summary_file(OTHER_ROWS)), "file")
    db.commit()
    assert extra(None) == [] and extra(his.id) == []
    assert statements.compare(db, account=his.id)[0].status == "ok"


def test_report_outside_the_summary_period_is_not_listed(db):
    _two_cabinets(db)
    report = make_report(db, platform="wb", period=(date(2026, 10, 12), date(2026, 10, 18)), key="SR-new")
    report.report_number = "900000001"
    db.commit()
    assert statements.not_in_summary(db, "wb", statements.compare(db)) == []


def test_upload_warns_about_other_legal_entity_and_mixed_file(db):
    her, his = _two_cabinets(db)
    other = _parse(_summary_file(OTHER_ROWS))
    warnings = statements.entity_warnings(db, "wb", her.id, other)
    assert len(warnings) == 1 and OTHER in warnings[0] and "ИП Яворская Т. Н." in warnings[0]
    assert statements.entity_warnings(db, "wb", his.id, other) == []
    assert statements.entity_warnings(db, "wb", her.id, _parse(_summary_file())) == []   # тот же свод ещё раз
    mixed = _parse(_summary_file(REAL_ROWS + OTHER_ROWS))
    assert "несколько юрлиц" in statements.entity_warnings(db, "wb", None, mixed)[0]


def test_summary_fills_wb_total_into_reports_loaded_without_it(db):
    report = _wb_report(db, "852719850", [{"operation": v.SALE, "quantity": 1, "payout": 765015}])
    assert report.declared_payout_kop is None
    stats = statements.store(db, "wb", None, _parse(_summary_file()), "file")
    db.commit()
    db.refresh(report)
    assert stats["declared_set"] == 1 and report.declared_payout_kop == 717886


def test_file_import_takes_the_wb_total_from_the_summary(db):
    statements.store(db, "wb", None, _parse(_summary_file()), "file")
    db.commit()
    make_product(db)
    outcome = import_report(db, platform="wb", file_bytes=wb_file([wb_row()]), user="admin",
                            filename="Еженедельный детализированный отчет №852719850_531247 - 1.xlsx")
    assert outcome.report.report_number == "852719850"          # номер — из имени файла
    assert outcome.report.declared_payout_kop == 717886


def test_api_list_is_stored_as_summary_without_wiping_file_columns(db):
    statements.store(db, "wb", None, _parse(_summary_file()), "file")
    db.commit()
    account = make_account(db)
    db.add(ApiCredential(account_id=account.id, field_name="finance_token", encrypted_value=encrypt_value("t")))
    db.commit()
    header = dict(WB_HEADER, reportId=852719850, dateFrom="2026-09-14", dateTo="2026-09-20",
                  reportType=2, bankPaymentSum="7178.86")
    session = FakeSession({wb_source.LIST_PATH: [header],
                           wb_source.DETAIL_PATH.format(report_id=852719850): [_wb_line(reportId=852719850)]})
    fetch.request_fetch(db, "wb_api", "admin", account.id, date(2026, 9, 1), date(2026, 9, 30))
    db.commit()
    fetch.run_pending(db, session=session, sleep=lambda s: None)
    statement = db.query(PlatformStatement).filter(PlatformStatement.report_number == "852719850").one()
    assert (statement.source, statement.account_id, statement.report_kind) == ("api", account.id, "по выкупам")
    assert statement.total_kop == 717886
    assert statement.storage_kop == 0                            # из файла, API его не затёр


def test_api_list_gives_every_article_so_a_doubled_expense_is_seen(db):
    """Список finance-api отдаёт итоги шапки по каждой статье (спецификация
    `sales-reports/list`): по ним у отчёта из API видно, КАКАЯ статья разошлась,
    а не только что итог не тот. До 19.10.2026 брались лишь доставка и итог."""
    header = dict(WB_HEADER, sellerFinanceName="ИП Яворская Т. Н.", forPaySum="2100",
                  deliveryServiceSum="150.5", paidStorageSum="40", paidAcceptanceSum="10",
                  deductionSum="300", penaltySum="5", additionalPaymentSum="2",
                  cashbackAmountSum="3", cashbackCommissionChangeSum="1", paymentSchedule="0",
                  bankPaymentSum="1588.50")
    row = statements.parse_api_list([header])[0]
    assert (row["storage_kop"], row["acceptance_kop"], row["penalty_kop"]) == (4000, 1000, 500)
    assert row["deduction_kop"] == 30600                         # 300 + 2 + 3 + 1 + 0
    assert row["legal_entity"] == "ИП Яворская Т. Н."
    assert row["for_pay_kop"] - row["delivery_kop"] - row["storage_kop"] - row["acceptance_kop"] \
        - row["deduction_kop"] - row["penalty_kop"] == row["total_kop"]
    account = make_account(db)
    statements.store(db, "wb", account.id, [row], "api")
    db.commit()
    # Хранение легло дважды: итог разошёлся ровно на него, и статья названа.
    _wb_report(db, "307401554", [{"operation": v.SALE, "quantity": 1, "payout": 210000},
                                 {"operation": v.LOGISTICS, "quantity": 0, "logistics": 15050},
                                 {"operation": v.STORAGE, "quantity": 0, "storage": 4000},
                                 {"operation": v.STORAGE, "quantity": 0, "storage": 4000},
                                 {"operation": v.ACCEPTANCE, "quantity": 0, "acceptance": 1000},
                                 {"operation": v.PENALTY, "quantity": 0, "penalty": 500},
                                 {"operation": v.DEDUCTION, "quantity": 0, "deduction": 30600}])
    found = {r.statement.report_number: r for r in statements.compare(db)}["307401554"]
    assert found.status == "diff" and found.diff == -4000
    assert found.bucket_diffs() == [("Хранение", 4000, 8000)]


# ------------------------------------------------------------ проверка доступа

def _account_with(db, platform, field, value):
    account = make_account(db, platform=platform, name=platform)
    db.add(ApiCredential(account_id=account.id, field_name=field, encrypted_value=encrypt_value(value)))
    db.commit()
    return account


def test_check_access_wb_ok_and_explained_refusals(db):
    account = _account_with(db, "wb", "finance_token", "t")
    ok, message = fetch.check_access(db, account, session=FakeSession({wb_source.LIST_PATH: [WB_HEADER]}),
                                     sleep=lambda s: None)
    assert ok and "отчётов за 14 дней — 1" in message
    session = FakeSession({"*": FakeResponse(403, {"detail": "scope is not allowed"})})
    ok, message = fetch.check_access(db, account, session=session, sleep=lambda s: None)
    assert not ok and "Финансы" in message
    assert len(session.calls) == 1                               # один запрос, не обход


def test_check_access_without_token_and_for_file_platforms(db):
    assert fetch.check_access(db, make_account(db))[0] is False
    assert "файлом" in fetch.check_access(db, make_account(db, platform="lamoda", name="L"))[1]


def test_check_access_kit(db):
    account = _account_with(db, "kit", "token", "k")
    session = FakeSession({"/v1/orders": {"orders": [], "total_count": 42}})
    ok, message = fetch.check_access(db, account, session=session, sleep=lambda s: None)
    assert ok and "42" in message
    assert session.headers["Authorization"] == "Bearer k"


def test_statements_page_and_upload(client, db):
    assert client.get("/finance/statements").status_code == 200
    response = client.post("/finance/statements/upload", files={"file": ("svod.xlsx", _summary_file())},
                           follow_redirects=True)
    assert "Свод загружен: отчётов 3" in response.text
    assert "848253942" in response.text and "не загружен" in response.text
    assert db.query(PlatformStatement).count() == 3


def test_statements_page_by_cabinet(client, db):
    her, his = _two_cabinets(db)
    response = client.post("/finance/statements/upload", data={"account_id": str(her.id)},
                           files={"file": ("svod.xlsx", _summary_file(OTHER_ROWS))}, follow_redirects=True)
    # Свод Яворского по ошибке загружен в кабинет Яворской — предупреждение.
    assert "уже лежит свод юрлица" in response.text
    response = client.post("/finance/statements/upload", data={"account_id": str(his.id)},
                           files={"file": ("svod.xlsx", _summary_file(OTHER_ROWS))}, follow_redirects=True)
    assert "юрлицо: ИП Яворский" in response.text and "уже лежит" not in response.text
    page = client.get(f"/finance/statements?account={his.id}").text
    assert "848966494" in page and "848253942" not in page and OTHER in page
    page = client.get("/finance/statements").text
    assert "848966494" in page and "848253942" in page and "своды нескольких кабинетов" in page
    page = client.get(f"/finance/statements?account={statements.NO_ACCOUNT}").text
    assert "848253942" not in page and "У этого кабинета свода нет" in page
    assert client.get("/finance/statements?account=999").status_code == 200   # чужой id — как «все»


def test_check_button_on_accounts_page(client, db, monkeypatch):
    account = _account_with(db, "wb", "finance_token", "t")
    monkeypatch.setattr(fetch, "check_access", lambda db_, acc: (True, "доступ есть"))
    response = client.post(f"/api-keys/{account.id}/check", follow_redirects=True)
    assert "доступ есть" in response.text


def test_save_and_check_uses_the_token_just_typed(client, db, monkeypatch):
    """Аудит 18.10: проверка доступа отдельной формой стирала вписанный, но не
    сохранённый ключ. «Сохранить и проверить» сохраняет и проверяет им."""
    from finapp import accounts
    account = make_account(db)
    seen = []
    monkeypatch.setattr(fetch, "check_access",
                        lambda db_, acc: (seen.append(accounts.credentials(db_, acc).get("finance_token")),
                                          (True, "доступ есть"))[1])
    response = client.post(f"/api-keys/{account.id}", data={"name": "ИП Тест", "is_active": "1",
                                                           "finance_token": "new-token", "then_check": "1"},
                           follow_redirects=True)
    assert "Проверка доступа: доступ есть" in response.text
    assert seen == ["new-token"]
