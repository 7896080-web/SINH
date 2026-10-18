"""Lamoda: еженедельные отчёты (предварительно) и коррекция отчётом комиссионера;
забор отчётов с почты.

Файлы собраны в раскладке настоящих: «AWER - Weekly Report - 2026-10-05.xlsx»
(лист сводки + «Продажи по размерам», заголовки слово в слово) и отчёт
комиссионера № CR000179664 за сентябрь 2026."""
import io
from datetime import date, datetime
from email.message import EmailMessage

import pytest
from openpyxl import Workbook

from finapp import mail
from finapp.finance import lamoda, values as v
from finapp.finance.importer import import_report
from finapp.finance.sales_1c import upload_problems
from finapp.models import MailMessage, SalesReport, SalesReportLine
from tests.finance_factories import make_account
from tests.test_finance_parsing import _lamoda_file, _lamoda_row

SIZE_HEADERS = ["", "Наименование личного кабинета", "Brand name", "Дата продажи", "Неделя",
                "Артикул Партнера", "Размер Партнера", "Короткий артикул Lamoda", "Размер Lamoda",
                "Наименование товара", "Категория", "Продано товаров", "Продано по акции", "Цена без скидки",
                "Цена со скидкой", "Стоимость купона", "Купон за счет партнера",
                "Скидка по программе лояльности", "Цена, согласованная с партнером", "Цена продажи"]


def _sale(day, article="C23-2251C Авер Куртка  48 черный", qty=1, agreed=10000):
    return [None, "AWER", "Awer", datetime(day.year, day.month, day.day), day.isocalendar()[1], article, "48",
            "MP002XM05TLK", "48", "Куртка", "OUTERWEAR", qty, "да", 20000, 12000, 0, None, 0, agreed, agreed]


def _weekly_file(sales, revenue=(100000,), commission=(42000,)):
    wb = Workbook()
    summary = wb.active
    summary.title = "Еженедельный отчет"
    summary.append([])
    summary.append([None, "AWER / Awer"])
    summary.append([None, None, "Даты", None, "Изменения за неделю, %", None, "28.09.26 - 04.10.26"])
    summary.append([None, None, "Выручка", None, 0.07, None, *revenue])
    summary.append([None, None, "Комиссия", None, 0.07, None, *commission])
    sizes = wb.create_sheet("Продажи по размерам")
    sizes.append(SIZE_HEADERS)
    for row in sales:
        sizes.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _import(db, data, account, name="AWER - Weekly Report - 2026-10-05.xlsx"):
    return import_report(db, platform="lamoda", file_bytes=data, filename=name, user="t", account_id=account.id)


def _sept_payout(db):
    lines = db.query(SalesReportLine).join(SalesReport).filter(
        SalesReport.platform == "lamoda",
        SalesReportLine.operation_date.between(date(2026, 9, 1), date(2026, 9, 30)))
    return sum(l.payout_kop for l in lines)


def test_weekly_is_split_into_provisional_weeks_with_commission_from_summary(db):
    account = make_account(db, platform="lamoda", name="AWER")
    data = _weekly_file([_sale(date(2026, 9, 29)), _sale(date(2026, 10, 1), qty=2, agreed=24818),
                         _sale(date(2026, 9, 22))])
    outcome = _import(db, data, account)
    assert outcome.errors == []
    weeks = db.query(SalesReport).order_by(SalesReport.period_from).all()
    assert [(w.period_from, w.period_to, w.report_kind) for w in weeks] == [
        (date(2026, 9, 21), date(2026, 9, 27), "предварительный"),
        (date(2026, 9, 28), date(2026, 10, 4), "предварительный")]
    line = db.query(SalesReportLine).filter(SalesReportLine.quantity == 2).one()
    # Цена — за всю строку; комиссия — по сводке (42000 / 100000).
    assert (line.revenue_kop, line.commission_kop, line.payout_kop) == (2481800, 1042356, 2481800 - 1042356)
    assert "42.00% (по сводке отчёта)" in outcome.warnings[0]


def test_next_weekly_letter_replaces_the_same_weeks(db):
    account = make_account(db, platform="lamoda", name="AWER")
    _import(db, _weekly_file([_sale(date(2026, 9, 29))]), account)
    _import(db, _weekly_file([_sale(date(2026, 9, 29)), _sale(date(2026, 9, 30))]), account, name="w2.xlsx")
    assert db.query(SalesReport).count() == 1
    assert db.query(SalesReportLine).count() == 2


def test_commission_report_replaces_provisional_data_for_its_period(db):
    account = make_account(db, platform="lamoda", name="AWER")
    _import(db, _weekly_file([_sale(date(2026, 9, 29)), _sale(date(2026, 10, 2)), _sale(date(2026, 9, 10))]),
            account)
    official = _lamoda_file([_lamoda_row(sold="20.09.2026")], total=10962)
    outcome = import_report(db, platform="lamoda", file_bytes=official, filename="Отчет комиссионера.xlsx",
                            user="t", account_id=account.id)
    assert any("Коррекция" in w and "удалено строк 2" in w for w in outcome.warnings)
    assert not any("пересекается" in w for w in outcome.warnings)
    # Сентябрь — только по отчёту комиссионера; неделя 7–13.09 опустела и удалена,
    # у недели 28.09–4.10 осталась октябрьская строка.
    assert _sept_payout(db) == 1096200
    weeks = db.query(SalesReport).filter(SalesReport.report_kind == lamoda.PROVISIONAL).all()
    assert [(w.period_from, w.lines_count) for w in weeks] == [(date(2026, 9, 28), 1)]

    # Письмо, пришедшее позже, закрытый месяц не трогает.
    again = _import(db, _weekly_file([_sale(date(2026, 9, 29)), _sale(date(2026, 10, 2))]), account, name="w3.xlsx")
    assert "закрытые отчётом комиссионера, пропущено: 1" in again.warnings[0]
    assert _sept_payout(db) == 1096200


def test_weekly_barcode_comes_from_commission_reports_by_partner_article(db):
    account = make_account(db, platform="lamoda", name="AWER")
    import_report(db, platform="lamoda", file_bytes=_lamoda_file([_lamoda_row(sold="20.09.2026")]),
                  filename="cr.xlsx", user="t", account_id=account.id)
    # Пробелы в «Артикуле Партнера» — как попало (в настоящих файлах двойные).
    _import(db, _weekly_file([_sale(date(2026, 10, 2), article="C23-2251C Авер Куртка 48 черный")]), account)
    week = db.query(SalesReport).filter(SalesReport.report_kind == lamoda.PROVISIONAL).one()
    assert week.lines[0].barcode == "2000932310718"


def test_provisional_week_is_never_sent_to_1c(db, epf_ready):
    account = make_account(db, platform="lamoda", name="AWER")
    _import(db, _weekly_file([_sale(date(2026, 10, 2))]), account)
    week = db.query(SalesReport).one()
    assert any("отчёт комиссионера" in p for p in upload_problems(db, week, "Lamoda_Склад"))


# ------------------------------------------------------------ почта

class FakeImap:
    """IMAP-сервер в памяти: проверяем, что ящик открыт только на чтение и
    письма читаются без пометки «прочитано»."""

    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user, password))
        return "OK", [b""]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.calls.append(("search",) + criteria)
        return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]

    def fetch(self, num, what):
        self.calls.append(("fetch", num, what))
        assert "PEEK" in what
        raw = self.messages[int(num) - 1].as_bytes()
        if "HEADER" in what:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]

    def logout(self):
        self.calls.append(("logout",))


def _letter(attachment: bytes, name: str, mid: str = "<1@lamoda.ru>"):
    msg = EmailMessage()
    msg["From"] = "Lamoda B2B <b2b@lamoda.ru>"
    msg["Subject"] = "AWER - Weekly Report - 2026-10-05"
    msg["Message-ID"] = mid
    msg["Date"] = "Mon, 05 Oct 2026 09:00:00 +0300"
    msg.set_content("Отчёт во вложении")
    msg.add_attachment(attachment, maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=name)
    return msg


def _configure(db, account):
    mail.save(db, host="imap.example.ru", port="993", user="reports@example.ru", password="app-pass",
              folder="INBOX", sender="b2b@lamoda.ru", account_id=str(account.id))
    db.commit()


def test_mail_imports_weekly_attachment_read_only_and_once(db):
    account = make_account(db, platform="lamoda", name="AWER")
    _configure(db, account)
    imap = FakeImap([_letter(_weekly_file([_sale(date(2026, 10, 2))]), "AWER - Weekly Report - 2026-10-05.xlsx")])
    stats = mail.fetch_new(db, connect=lambda h, p: imap)
    assert stats["imported"] == 1
    assert ("select", '"INBOX"', True) in imap.calls          # только чтение
    assert ("login", "reports@example.ru", "app-pass") in imap.calls
    search = next(c for c in imap.calls if c[0] == "search")
    assert search[1:3] == ("FROM", '"b2b@lamoda.ru"') and search[3] == "SINCE"
    record = db.query(MailMessage).one()
    assert record.status == "imported" and record.received_at == datetime(2026, 10, 5, 6, 0)
    assert db.query(SalesReport).filter(SalesReport.report_kind == lamoda.PROVISIONAL).count() == 1

    # Второй проход: письмо уже разобрано — тело не скачивается.
    imap.calls.clear()
    assert mail.fetch_new(db, connect=lambda h, p: imap)["messages"] == 0
    assert not [c for c in imap.calls if c[0] == "fetch" and c[2] == "(BODY.PEEK[])"]


def test_mail_with_commission_report_applies_the_correction(db):
    account = make_account(db, platform="lamoda", name="AWER")
    _configure(db, account)
    _import(db, _weekly_file([_sale(date(2026, 9, 29))]), account)
    imap = FakeImap([_letter(_lamoda_file([_lamoda_row(sold="20.09.2026")]), "Отчет комиссионера 30.09.26.xlsx",
                             mid="<2@lamoda.ru>")])
    mail.fetch_new(db, connect=lambda h, p: imap)
    assert "Коррекция" in db.query(MailMessage).one().note
    assert _sept_payout(db) == 1096200


def test_mail_not_configured_does_nothing(db):
    assert mail.fetch_new(db, connect=lambda h, p: pytest.fail("не должно подключаться"))["configured"] is False


def test_mail_login_failure_is_explained(db):
    import imaplib
    account = make_account(db, platform="lamoda", name="AWER")
    _configure(db, account)

    class Refusing(FakeImap):
        def login(self, user, password):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    with pytest.raises(mail.MailError, match="вход не удался"):
        mail.fetch_new(db, connect=lambda h, p: Refusing([]))


def test_mail_password_is_stored_encrypted_and_kept_when_blank(db):
    from finapp import settings
    account = make_account(db, platform="lamoda", name="AWER")
    _configure(db, account)
    assert "app-pass" not in settings.get(db, mail.PASSWORD)
    mail.save(db, host="imap.example.ru", port="993", user="reports@example.ru", password="", folder="INBOX",
              sender="b2b@lamoda.ru", account_id=str(account.id))
    assert mail.load(db).password == "app-pass"


def test_since_uses_english_month_names():
    assert mail._since(0).split("-")[1] in mail._MONTHS


@pytest.mark.parametrize("first_with_account", [False, True])
def test_same_week_from_mail_and_by_hand_is_one_report(db, first_with_account):
    """Аудит 18.10: неделя с почты (кабинет не указан) и та же неделя руками с
    кабинетом ложились двумя отчётами — продажи недели вдвое до отчёта
    комиссионера. Теперь — один, с указанным кабинетом."""
    account = make_account(db, platform="lamoda", name="AWER")
    data = _weekly_file([_sale(date(2026, 10, 2))])
    by_hand = lambda: import_report(db, platform="lamoda", file_bytes=data, filename="AWER - Weekly Report - 2026-10-05.xlsx",
                                    user="t", account_id=account.id)
    by_mail = lambda: import_report(db, platform="lamoda", file_bytes=data + b"", filename="w.xlsx",
                                    user="почта", account_id=None, source="email")
    for step in ((by_hand, by_mail) if first_with_account else (by_mail, by_hand)):
        # тот же файл дважды отсечётся по sha — второй раз загружаем копию с другим именем
        db.query(SalesReport).update({SalesReport.file_sha256: None})
        db.commit()
        step()
    weeks = db.query(SalesReport).filter(SalesReport.report_kind == lamoda.PROVISIONAL).all()
    assert len(weeks) == 1 and weeks[0].account_id == account.id
