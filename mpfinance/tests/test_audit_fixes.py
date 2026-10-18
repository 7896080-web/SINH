"""Исправления по аудиту 18.10.2026 (`АУДИТ_2026-10-18.md`): каждая проверка —
про следствие, которое было неверным до правки."""
from datetime import date, timedelta
from decimal import Decimal

from finapp.finance import values as v
from finapp.models import CurrencyRate, OnecTask, OneCUploadStatus, ProductCost
from finapp.timeutils import today_local
from tests.finance_factories import make_product, make_report


def _cost(db, uid, day, minor, currency, source):
    db.add(ProductCost(uid_1c=uid, valid_from=day, cost_minor=minor, currency=currency, source=source))


def test_manual_cost_beats_a_newer_1c_cost(db):
    """Цена из Excel без даты ложится с 01.01.2000, а выгрузка 1С — с сегодня.
    Раньше продажа брала «последнюю запись не позже даты», то есть 1С."""
    from finapp.finance.economics import CostBook
    make_product(db)
    db.add(CurrencyRate(rate_date=date(2026, 10, 5), usd_rub_e4=900000))
    _cost(db, "u1", date(2000, 1, 1), 150000, "RUB", "excel")
    _cost(db, "u1", date(2026, 10, 5), 1000, "USD", "1c")
    db.commit()
    assert CostBook(db, {"u1"}).cost_on("u1", date(2026, 10, 10)) == 150000


def test_dated_manual_cost_applies_from_its_date_only(db):
    from finapp.finance.economics import CostBook
    make_product(db)
    _cost(db, "u1", date(2026, 9, 1), 80000, "RUB", "1c")
    _cost(db, "u1", date(2026, 10, 1), 150000, "RUB", "excel")
    db.commit()
    book = CostBook(db, {"u1"})
    assert book.cost_on("u1", date(2026, 9, 15)) == 80000      # до ручной цены — 1С
    assert book.cost_on("u1", date(2026, 10, 15)) == 150000
    assert book.cost_on("u1", date(2026, 1, 1)) == 150000      # раньше всех записей — ручная


def test_1c_export_leaves_a_manual_cost_alone(db):
    from finapp.finance.costs import import_costs
    make_product(db)
    _cost(db, "u1", date(2000, 1, 1), 150000, "RUB", "excel")
    _cost(db, "u1", date(2026, 10, 5), 1000, "USD", "1c")
    db.commit()
    stats = import_costs(db, {"u1": 1200})
    assert (stats["manual"], stats["updated"]) == (1, 0)


def test_stale_cbr_rate_is_not_used_silently(db):
    from finapp.finance.rates import RateBook
    db.add(CurrencyRate(rate_date=date(2026, 9, 1), usd_rub_e4=800000))
    db.commit()
    assert RateBook(db).rate_on(date(2026, 9, 7)) == Decimal(80)          # выходные/праздники — да
    assert RateBook(db).rate_on(date(2026, 10, 15)) is None                # полтора месяца — нет
    assert RateBook(db, manual=Decimal(95)).rate_on(date(2026, 10, 15)) == Decimal(95)


def test_cbr_answer_in_another_markup_is_an_error():
    import pytest

    from finapp.finance.rates import RateError, parse_cbr_dynamic
    with pytest.raises(RateError):
        parse_cbr_dynamic(b'<ValCurs><Item Date="01.09.2026"><Value>80</Value></Item></ValCurs>')
    assert parse_cbr_dynamic(b"<ValCurs ID='R01235'></ValCurs>") == {}    # выходные — пусто, не ошибка


def test_api_answers_of_another_shape_are_errors():
    import pytest
    import requests

    from finapp.finance.sources import kit as kit_source
    from finapp.finance.sources import wb as wb_source
    from tests.test_finance_api_fetch import FakeSession
    with pytest.raises(wb_source.WbFinanceError, match="не списком"):
        wb_source.WbFinanceApi("t", session=FakeSession({wb_source.LIST_PATH: {"data": []}}),
                               sleep=lambda s: None).list_reports(date(2026, 9, 1), date(2026, 9, 7))
    with pytest.raises(RuntimeError, match="нет списка заказов"):
        kit_source.KitOrdersApi("t", session=FakeSession({"/v1/orders": {"done": []}}),
                                sleep=lambda s: None).orders_updated_since(
            __import__("datetime").datetime(2026, 9, 1, tzinfo=__import__("datetime").timezone.utc))


def test_disabling_1c_withdraws_what_has_not_left(client, db, onec_on):
    report = make_report(db, key="SR-q")
    report.onec_status = OneCUploadStatus.pending
    db.add(OnecTask(command="FIN_PING", key="FINPING-1", line="x", status="pending"))
    db.add(OnecTask(command="BARCODE_DICT", key="FINDICT-2", line="x", status="sent"))
    db.commit()
    response = client.post("/diagnostics/onec-enabled", data={"enabled": "0"}, follow_redirects=True)
    assert "сняты с очереди" in response.text
    db.expire_all()
    assert report.onec_status == OneCUploadStatus.none
    statuses = {t.command: t.status for t in db.query(OnecTask)}
    assert statuses == {"FIN_PING": "failed", "BARCODE_DICT": "sent"}      # ушедшее не трогаем


def test_no_new_1c_command_while_the_last_one_went_unanswered(db, epf_ready):
    from finapp import settings
    from finapp.workers import jobs
    db.add(OnecTask(command="BARCODE_DICT", key="FINDICT-1", line="x", status="timeout"))
    db.add(OnecTask(command="EXPORT_COST_PRICES", key="FINCOST-2", line="x", status="done"))
    db.commit()
    jobs.job_daily()
    db.expire_all()
    commands = [t.command for t in db.query(OnecTask).filter(OnecTask.status == "pending")]
    assert commands == ["EXPORT_COST_PRICES"]


def test_backup_leftovers_are_swept(db):
    import os
    import time

    from finapp import backup, config
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    part = config.BACKUP_DIR / "mpfinance-20261018-100000.part"
    old_bad = config.BACKUP_DIR / "mpfinance-20261010-100000.bad"
    new_bad = config.BACKUP_DIR / "mpfinance-20261018-100001.bad"
    for f in (part, old_bad, new_bad):
        f.write_bytes(b"x")
    two_days = time.time() - 2 * 86400
    os.utime(old_bad, (two_days, two_days))
    assert backup.sweep_leftovers() == 2
    assert not part.exists() and not old_bad.exists() and new_bad.exists()   # свежую .bad — на разбор
    new_bad.unlink()


def test_broken_attachment_does_not_block_the_mailbox(db, monkeypatch):
    """Неожиданный сбой разбора вложения записывается в письмо, а не роняет
    проход: следующие письма разбираются."""
    from finapp import mail
    from finapp.finance import importer
    from finapp.models import MailMessage
    from tests.finance_factories import make_account
    from tests.test_finance_lamoda import FakeImap, _configure, _letter
    _configure(db, make_account(db, platform="lamoda", name="AWER"))
    imap = FakeImap([_letter(b"PK", "плохой.xlsx", "<a@x>"), _letter(b"PK", "хороший.xlsx", "<b@x>")])
    calls = []

    def flaky(db_, **kw):
        calls.append(kw["filename"])
        if kw["filename"] == "плохой.xlsx":
            raise KeyError("worksheet")
        return importer.ImportOutcome(errors=["не опознан"])
    monkeypatch.setattr(importer, "import_report", flaky)
    stats = mail.fetch_new(db, connect=lambda h, p: imap)
    assert calls == ["плохой.xlsx", "хороший.xlsx"]
    assert db.query(MailMessage).count() == 2 and stats["messages"] == 2


# ------------------------------------------------------------ ответ 1С и отправка

def test_late_answer_to_a_previous_send_does_not_touch_the_current_one(db):
    """Отчёт отправлен повторно (вторым файлом). Поздний ERROR на первую
    отправку не объявляет ошибку, а OK на вторую — принимается."""
    from finapp.finance import sales_1c
    report = make_report(db, key="SR-wb-777")
    report.onec_status = OneCUploadStatus.sent
    report.onec_batch_filename = "task_fin_second.txt"
    db.commit()
    assert sales_1c.apply_result(db, "SR-wb-777", "ERROR", "старый отказ", "task_fin_first.txt") is False
    assert report.onec_status == OneCUploadStatus.sent
    assert sales_1c.apply_result(db, "SR-wb-777", "OK", "ЦБ-1", "task_fin_second.txt") is True
    assert report.onec_status == OneCUploadStatus.done


def test_unmatched_1c_answer_goes_to_the_action_log(db, exchange_dirs):
    from finapp import onec
    from finapp.exchange import LocalExchange
    from finapp.models import AuditLog
    (exchange_dirs.ONEC_RESULTS_DIR / "result_fin_x.txt").write_text(
        "SR-wb-nothing|OK|ЦБ-1|SALES_REPORT\n", encoding="utf-8")
    ex = LocalExchange(exchange_dirs.ONEC_TASKS_DIR, exchange_dirs.ONEC_RESULTS_DIR, exchange_dirs.ONEC_ARCHIVE_DIR)
    total = onec.collect_results(db, ex)
    assert total["unmatched"] == 1
    assert db.query(AuditLog).filter(AuditLog.action == "onec_unmatched").count() == 1


def test_report_1c_did_not_answer_is_not_replaced_by_a_refetch(db):
    """«1С не ответила» — документ мог создаться: перезабор его не заменяет."""
    from finapp.finance.formats import ParseResult
    from finapp.finance.fetch import KIT_API_FORMAT
    from finapp.finance.importer import store_api_report
    from tests.finance_factories import make_account
    account = make_account(db, platform="kit", name="Сайт")
    line = dict(v.empty_line(), operation=v.LOGISTICS, logistics=25000, operation_date=date(2026, 9, 8))
    first = store_api_report(db, platform="kit", account_id=account.id,
                             parsed=ParseResult(format=KIT_API_FORMAT, lines=[dict(line)]),
                             period_from=date(2026, 9, 7), period_to=date(2026, 9, 13), user="t",
                             replace_unsent=True, kind="kitdlv").report
    first.onec_status = OneCUploadStatus.timeout
    db.commit()
    again = store_api_report(db, platform="kit", account_id=account.id,
                             parsed=ParseResult(format=KIT_API_FORMAT, lines=[dict(line)]),
                             period_from=date(2026, 9, 7), period_to=date(2026, 9, 13), user="t",
                             replace_unsent=True, kind="kitdlv")
    assert again.skipped and again.report.id == first.id


# ------------------------------------------------------------ свежесть загрузок

def test_account_freshness_names_a_rejected_key_and_staleness(db):
    from finapp.crypto import encrypt_value
    from finapp.finance import fetch
    from finapp.models import ApiCredential, ReportFetch
    from finapp.timeutils import now_utc
    from tests.finance_factories import make_account
    wb = make_account(db, name="WB Яворская")
    db.add(ApiCredential(account_id=wb.id, field_name="finance_token", encrypted_value=encrypt_value("t")))
    db.add(ReportFetch(kind="wb_api", account_id=wb.id, status="done", finished_at=now_utc() - timedelta(days=12)))
    db.add(ReportFetch(kind="wb_api", account_id=wb.id, status="failed", message="WB: 401 Unauthorized",
                       finished_at=now_utc() - timedelta(days=1)))
    kit = make_account(db, platform="kit", name="Сайт")
    db.commit()
    # У кабинета KIT строк две: загрузка по API и продажи сайта из базы AWER CRM.
    rows = {(f["account"].name, f["source"]): f for f in fetch.account_freshness(db)}
    wb_row, kit_row = rows[("WB Яворская", "wb")], rows[("Сайт", "kit")]
    assert wb_row["stale"] and "не приняла ключ" in wb_row["problem"]
    assert wb_row["days"] == 12
    assert kit_row["stale"] and "ключ не задан" in kit_row["problem"]
    assert "kitcrm.db" in rows[("Сайт", "crm")]["problem"]


def test_fresh_account_is_ok_and_pages_show_freshness(client, db):
    from finapp.crypto import encrypt_value
    from finapp.models import ApiCredential, ReportFetch
    from finapp.timeutils import now_utc
    from tests.finance_factories import make_account
    wb = make_account(db, name="WB Яворский")
    db.add(ApiCredential(account_id=wb.id, field_name="finance_token", encrypted_value=encrypt_value("t")))
    db.add(ReportFetch(kind="wb_api", account_id=wb.id, status="done", finished_at=now_utc() - timedelta(days=2)))
    stale = make_account(db, name="WB старый")
    db.add(ApiCredential(account_id=stale.id, field_name="finance_token", encrypted_value=encrypt_value("t")))
    db.add(ReportFetch(kind="wb_api", account_id=stale.id, status="failed", message="403 Forbidden",
                       finished_at=now_utc()))
    db.commit()
    page = client.get("/diagnostics").text
    assert "Загрузки по кабинетам" in page and "WB Яворский" in page and "не приняла ключ" in page
    dash = client.get("/finance").text
    assert "WB старый" in dash and "не приняла ключ" in dash and "WB Яворский: " not in dash


def test_not_in_1c_counts_only_reports_that_go_to_1c(client, db):
    """Предварительные недели Lamoda и доставка KIT в 1С не уходят никогда —
    «не загружено в 1С» их не считает, а обычный отчёт (вид пуст) — считает."""
    period = (today_local() - timedelta(days=3), today_local())
    sale = [{"operation": v.SALE, "quantity": 1, "revenue": 100000, "payout": 80000}]
    make_report(db, key="SR-plain", period=period, lines=sale)
    weekly = make_report(db, platform="lamoda", key="SR-week", period=period, lines=sale)
    weekly.report_kind = "предварительный"
    db.commit()
    dash = client.get("/finance").text
    assert "не загруженных в 1С: 1 из 2" in dash
