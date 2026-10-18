"""Партнёрский отчёт Lamoda — справочник «артикул Lamoda → EAN», а не продажи.

С 14.09.2026 Lamoda присылает на ту же почту «Lamoda Sales Report - AWER
ГГГГ-ММ-ДД.xlsx»: аналитика по артикулам (продажи за 7/14/28/180 дней, сток) с
полным и коротким артикулом Lamoda, размером партнёра и EAN в одной строке.
Программа его не знала — восемь писем подряд легли с «Отчёт не опознан», — а
недельные строки (короткий артикул + размер, EAN нет) не находили товар 1С: в
«Мэппинге» Sync Admin артикулов Lamoda не было ни одного. Файл собран в
раскладке настоящего (отчёт от 06.10.2026, лист «Артикулы», шапка с «Дата
отчета:»), заголовки слово в слово."""
import io
import sqlite3
from datetime import date, datetime

from openpyxl import Workbook

from finapp import mail
from finapp.finance import lamoda, values as v
from finapp.finance.importer import import_report, resolve_uids
from finapp.models import LamodaArticle, MailMessage, SalesReport
from tests.finance_factories import make_account, make_report
from tests.test_cost_file import _alembic
from tests.test_finance_lamoda import FakeImap, _configure, _letter, _weekly_file

HEADERS = [None, "Наименование личного кабинета", "Бренд", "Наименование товара", "Артикул Партнера",
           "Размер Партнера", "Артикул Lamoda", "Короткий артикул Lamoda", "Размер Lamoda", "Пол",
           "EAN (штрихкод)", "Категория товара", "Коллекция", "Сезон товара", "Цвет", "Модель товара",
           "Продажи за последние 7 дней, шт.", "Продажи за последние 7 дней, руб.",
           "Поставлено товара за последние 7 дней, шт.", "На свободном стоке, шт."]


def _row(partner, size, full, short, ean, sold=None, rub=None):
    return [None, "AWER", "Awer", "Свитшот", partner, size, full, short, "50/52", "men", ean, "SWEATSHIRTS",
            "AW25", "мульти", "синий", None, sold, rub, None, 3]


INDIGO_XXL = _row("27643  INDIGO MEL 2XL базовый", "XXL", "MP002XM0WFXNINXXL", "MP002XM0WFXN", "2000931984941",
                  sold=2, rub=9000)


def _partner_file(rows, report_date=datetime(2026, 10, 6)):
    wb = Workbook()
    ws = wb.active
    ws.title = "Артикулы"
    ws.append([])
    ws.append([None, "Партнерский отчет - Артикулы"])
    ws.append([])
    ws.append([None, "Дата отчета:", report_date])
    ws.append([])
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    cats = wb.create_sheet("Категории")
    cats.append([None, "Наименование личного кабинета", "Бренд", "Категория товара",
                 "Продажи за последние 7 дней, шт."])
    cats.append([None, "AWER", "Awer", "SWEATSHIRTS", 2])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(db, data, name="Lamoda Sales Report - AWER 2026-10-07.xlsx"):
    return import_report(db, platform="lamoda", file_bytes=data, filename=name, user="t")


def _weekly_row(day, short, size, article="27643  INDIGO MEL 2XL базовый", agreed=450000):
    return [None, "AWER", "Awer", datetime(day.year, day.month, day.day), day.isocalendar()[1], article, size,
            short, "54/56", "Свитшот", "SWEATSHIRTS", 1, "нет", agreed, agreed, 0, None, 0, agreed, agreed]


def test_partner_report_is_taken_as_catalog_not_as_sales(db):
    outcome = _upload(db, _partner_file([INDIGO_XXL]))
    assert outcome.errors == [] and outcome.report is None and outcome.catalog["rows"] == 1
    assert db.query(SalesReport).count() == 0                  # продажи за 7 дней не взяты
    row = db.get(LamodaArticle, "MP002XM0WFXNINXXL")
    assert (row.short_sku, row.partner_size, row.ean, row.report_date) == \
        ("MP002XM0WFXN", "XXL", "2000931984941", date(2026, 10, 6))
    assert "справочник артикулов" in outcome.notes[0] and "не берутся" in outcome.notes[0]


def test_weekly_and_partner_reports_are_told_apart():
    from finapp.finance.readers import read_sheets
    weekly = read_sheets(_weekly_file([_weekly_row(date(2026, 10, 2), "MP002XM0WFXN", "XXL")]), "w.xlsx")
    partner = read_sheets(_partner_file([INDIGO_XXL]), "p.xlsx")
    assert lamoda.is_weekly(weekly) and not lamoda.is_catalog(weekly)
    assert lamoda.is_catalog(partner) and not lamoda.is_weekly(partner)


def test_weekly_line_finds_product_through_ean_and_at_once(db, sync_db):
    """Недельная строка без EAN: короткий артикул + размер партнёра → EAN из
    партнёрского отчёта → товар 1С. Пересопоставление — сразу при приёме
    справочника, не через час."""
    sync_db.product("u-indigo-xxl", article="27643", name="27643 МСЛ Свитшот базовый INDIGO MEL", size="2XL")
    sync_db.barcode("2000931984941", "u-indigo-xxl")
    account = make_account(db, platform="lamoda", name="AWER")
    import_report(db, platform="lamoda", file_bytes=_weekly_file([_weekly_row(date(2026, 10, 2), "MP002XM0WFXN", "XXL")]),
                  filename="AWER - Weekly Report - 2026-10-05.xlsx", user="t", account_id=account.id)
    db.commit()
    week = db.query(SalesReport).one()
    assert week.unmatched_lines == 1

    outcome = _upload(db, _partner_file([INDIGO_XXL]))
    db.expire_all()
    line = week.lines[0]
    assert (week.unmatched_lines, line.uid_1c, line.barcode) == (0, "u-indigo-xxl", "2000931984941")
    assert "нашли товар 1С: 1" in outcome.notes[0]


def test_full_article_comes_from_catalog_not_assembled(db, sync_db):
    """«2.5XL» у Lamoda — «…IN25XL», а не «…IN2.5XL»: собранный ключ
    промахнулся бы. EAN у строки справочника нет — ищется полный артикул."""
    _upload(db, _partner_file([_row("55045 10058 АВЕР Рубашка К/р бежевый 2.5XL", "2.5XL", "MP002XM052KEIN25XL",
                                    "MP002XM052KE", None)]))
    sync_db.product("u-shirt", article="55045", name="Рубашка", size="2.5XL")
    sync_db.barcode("MP002XM052KEIN25XL", "u-shirt", source="excel")
    report = make_report(db, platform="lamoda", period=(date(2026, 10, 5), date(2026, 10, 11)), key="SR-c1",
                         lines=[{"operation": v.SALE, "revenue": 300000, "payout": 174000, "barcode": None,
                                 "platform_sku": "MP002XM052KE", "size": "2.5XL"}])
    assert resolve_uids(db, report) == 0 and report.lines[0].uid_1c == "u-shirt"


def test_older_file_does_not_overwrite_and_blank_ean_keeps_the_known_one(db):
    _upload(db, _partner_file([INDIGO_XXL]))
    older = _row("27643  INDIGO MEL 2XL базовый", "XXL", "MP002XM0WFXNINXXL", "MP002XM0WFXN", "2000000000001")
    outcome = _upload(db, _partner_file([older], report_date=datetime(2026, 9, 14)), name="old.xlsx")
    assert outcome.catalog["older"] == 1
    assert db.get(LamodaArticle, "MP002XM0WFXNINXXL").ean == "2000931984941"
    blank = _row("27643  INDIGO MEL 2XL базовый", "XXL", "MP002XM0WFXNINXXL", "MP002XM0WFXN", None)
    _upload(db, _partner_file([blank], report_date=datetime(2026, 10, 13)), name="new.xlsx")
    row = db.get(LamodaArticle, "MP002XM0WFXNINXXL")
    assert (row.ean, row.report_date) == ("2000931984941", date(2026, 10, 13))


def test_two_articles_for_one_model_and_size_are_not_guessed(db, sync_db):
    _upload(db, _partner_file([
        _row("10058 XL", "XL", "MP002XM052KEINXL", "MP002XM052KE", "2000000000101"),
        _row("10058sb XL", "XL", "MP002XM052KEINXLSB", "MP002XM052KE", "2000000000102")]))
    sync_db.product("u-a", article="10058", size="XL")
    sync_db.barcode("2000000000101", "u-a")
    sync_db.product("u-b", article="10058sb", size="XL")
    sync_db.barcode("2000000000102", "u-b")
    report = make_report(db, platform="lamoda", period=(date(2026, 10, 5), date(2026, 10, 11)), key="SR-c2",
                         lines=[{"operation": v.SALE, "revenue": 1, "payout": 1, "barcode": None,
                                 "platform_sku": "MP002XM052KE", "size": "XL"},
                                {"operation": v.SALE, "revenue": 1, "payout": 1, "barcode": None,
                                 "platform_sku": "MP002XM052KEINXL"}])
    assert resolve_uids(db, report) == 1                        # полный артикул — однозначен
    assert [l.uid_1c for l in report.lines] == [None, "u-a"]


def test_mail_takes_partner_report_as_catalog(db):
    account = make_account(db, platform="lamoda", name="AWER")
    _configure(db, account)
    imap = FakeImap([_letter(_partner_file([INDIGO_XXL]), "Lamoda Sales Report - AWER 2026-10-07.xlsx")])
    assert mail.fetch_new(db, connect=lambda h, p: imap)["imported"] == 1
    record = db.query(MailMessage).one()
    assert record.status == "imported" and "справочник артикулов" in record.note
    assert db.query(LamodaArticle).count() == 1 and db.query(SalesReport).count() == 0


def test_upload_page_and_diagnostics(client, db):
    assert "Артикулы Lamoda → EAN" in client.get("/diagnostics").text
    response = client.post("/finance/reports/upload", data={"platform": "lamoda"},
                           files={"file": ("Lamoda Sales Report - AWER 2026-10-07.xlsx", _partner_file([INDIGO_XXL]))},
                           follow_redirects=True)
    assert "принят как справочник артикулов" in response.text
    assert "артикулов 1, с EAN 1" in client.get("/diagnostics").text


def test_migration_requeues_only_partner_report_letters(tmp_path):
    path = tmp_path / "m.db"
    url = f"sqlite:///{path}"
    _alembic(url, "upgrade", "0006")
    conn = sqlite3.connect(path)
    notes = [("<p@lamoda>", "failed", "Lamoda Sales Report - AWER 2026-10-07.xlsx: Отчёт не опознан. Колонки в "
                                       "файле: «Артикул Lamoda», «Короткий артикул Lamoda», «Размер Lamoda», "
                                       "«Пол», «EAN (штрихкод)», «Категория товара»"),
             ("<x@lamoda>", "failed", "x.xlsx: Отчёт не опознан. Колонки в файле: «Дата», «Сумма»"),
             ("<w@lamoda>", "imported", "AWER - Weekly Report - 2026-10-05.xlsx: загружен")]
    for mid, status, note in notes:
        conn.execute("INSERT INTO mail_messages (message_id, status, note) VALUES (?, ?, ?)", (mid, status, note))
    conn.commit()
    conn.close()
    _alembic(url, "upgrade", "head")
    conn = sqlite3.connect(path)
    left = sorted(r[0] for r in conn.execute("SELECT message_id FROM mail_messages"))
    logged = conn.execute("SELECT details FROM audit_log WHERE action = 'mail_requeue'").fetchone()
    conn.execute("SELECT lamoda_sku, short_sku, partner_size, ean FROM lamoda_articles").fetchall()
    conn.close()
    assert left == ["<w@lamoda>", "<x@lamoda>"]
    assert logged and logged[0].endswith(": 1")
    _alembic(url, "downgrade", "0006")
    _alembic(url, "upgrade", "head")
