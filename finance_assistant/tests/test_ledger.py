import csv
from datetime import date

import pytest

from finance.ledger import FinanceError, Ledger
from finance.money import format_amount, parse_amount


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger(str(tmp_path / "f.db"))
    lg.add_account("Карта", "10000")
    lg.add_account("Наличные")
    yield lg
    lg.close()


@pytest.mark.parametrize("text, kopecks", [
    ("1234,5", 123450), ("1 234.56", 123456), ("0.005", 1), ("-10", -1000), ("7", 700),
])
def test_parse_amount(text, kopecks):
    assert parse_amount(text) == kopecks


@pytest.mark.parametrize("text", ["", "abc", "1,2,3", "nan", "inf"])
def test_parse_amount_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_format_amount():
    assert format_amount(12345678) == "123 456,78"
    assert format_amount(-5) == "-0,05"


def test_default_categories_seeded_once(tmp_path):
    path = str(tmp_path / "x.db")
    first = len(Ledger(path).categories())
    assert first > 0
    assert len(Ledger(path).categories()) == first


def test_balances_follow_all_operation_kinds(ledger):
    ledger.add_income("50000", "Карта", "Зарплата", "2026-09-05")
    ledger.add_expense("1200,50", "Карта", "Продукты", "2026-09-06")
    ledger.add_transfer("3000", "Карта", "Наличные", "2026-09-07")
    ledger.add_expense("500", "Наличные", "Транспорт", "2026-09-08")
    assert ledger.balances() == {
        "Карта": (10000 + 50000 - 3000) * 100 - 120050,
        "Наличные": 250000,
    }


def test_category_kind_must_match_operation(ledger):
    with pytest.raises(FinanceError, match="доход"):
        ledger.add_expense("10", "Карта", "Зарплата")


@pytest.mark.parametrize("amount", ["0", "-5"])
def test_amount_must_be_positive(ledger, amount):
    with pytest.raises(FinanceError):
        ledger.add_expense(amount, "Карта", "Продукты")


def test_unknown_account_and_self_transfer(ledger):
    with pytest.raises(FinanceError, match="нет счёта"):
        ledger.add_expense("10", "Вклад", "Продукты")
    with pytest.raises(FinanceError):
        ledger.add_transfer("10", "Карта", "Карта")


def test_duplicate_account(ledger):
    with pytest.raises(FinanceError, match="уже есть"):
        ledger.add_account("Карта")


def test_russian_date_format(ledger):
    ledger.add_expense("10", "Карта", "Продукты", "3.9.2026")
    assert ledger.transactions()[0].date == "2026-09-03"
    with pytest.raises(FinanceError):
        ledger.add_expense("10", "Карта", "Продукты", "31.02.2026")


def test_month_summary_and_filters(ledger):
    ledger.add_income("100000", "Карта", "Зарплата", "2026-09-01")
    ledger.add_expense("30000", "Карта", "Продукты", "2026-09-10")
    ledger.add_expense("10000", "Карта", "Кафе и рестораны", "2026-09-30")
    ledger.add_expense("99999", "Карта", "Продукты", "2026-10-01")  # другой месяц
    ledger.add_transfer("5000", "Карта", "Наличные", "2026-09-15")  # не доход и не расход
    s = ledger.month_summary("2026-09")
    assert s["total_income"] == 10_000_000
    assert s["total_expense"] == 4_000_000
    assert s["net"] == 6_000_000
    assert s["savings_rate"] == 60.0
    assert list(s["expense"]) == ["Продукты", "Кафе и рестораны"]
    assert len(ledger.transactions(month="2026-09")) == 4
    assert len(ledger.transactions(category="Продукты")) == 2
    assert len(ledger.transactions(account="Наличные")) == 1


def test_savings_rate_none_without_income(ledger):
    ledger.add_expense("10", "Карта", "Продукты", "2026-09-01")
    assert ledger.month_summary("2026-09")["savings_rate"] is None


def test_trend_crosses_year_boundary(ledger):
    ledger.add_expense("10", "Карта", "Продукты", "2025-12-31")
    ledger.add_income("20", "Карта", "Зарплата", "2026-01-01")
    rows = ledger.trend(3, until="2026-01")
    assert [r["month"] for r in rows] == ["2025-11", "2025-12", "2026-01"]
    assert rows[1]["total_expense"] == 1000
    assert rows[2]["net"] == 2000


def test_delete_transaction(ledger):
    tx = ledger.add_expense("10", "Карта", "Продукты")
    ledger.delete_transaction(tx)
    assert ledger.transactions() == []
    with pytest.raises(FinanceError):
        ledger.delete_transaction(tx)


def test_budget_states(ledger):
    for cat, limit in (("Продукты", "10000"), ("Кафе и рестораны", "5000"), ("Транспорт", "3000")):
        ledger.set_budget(cat, limit, "2026-09")
    ledger.add_expense("11000", "Карта", "Продукты", "2026-09-02")
    ledger.add_expense("4000", "Карта", "Кафе и рестораны", "2026-09-03")  # 80% на 10-й день
    ledger.add_expense("300", "Карта", "Транспорт", "2026-09-03")
    status = {r["category"]: r for r in ledger.budget_status("2026-09", today=date(2026, 9, 10))}
    assert status["Продукты"]["state"] == "over"
    assert status["Продукты"]["left"] == -100000
    assert status["Кафе и рестораны"]["state"] == "fast"
    assert status["Транспорт"]["state"] == "ok"


def test_budget_only_for_expense_and_upsert(ledger):
    with pytest.raises(FinanceError):
        ledger.set_budget("Зарплата", "100", "2026-09")
    ledger.set_budget("Продукты", "100", "2026-09")
    ledger.set_budget("Продукты", "200", "2026-09")
    assert ledger.budget_status("2026-09")[0]["limit"] == 20000


def test_copy_budgets_keeps_existing(ledger):
    ledger.set_budget("Продукты", "100", "2026-09")
    ledger.set_budget("Транспорт", "50", "2026-09")
    ledger.set_budget("Транспорт", "70", "2026-10")
    assert ledger.copy_budgets("2026-09", "2026-10") == 1
    limits = {r["category"]: r["limit"] for r in ledger.budget_status("2026-10")}
    assert limits == {"Продукты": 10000, "Транспорт": 7000}


def test_recurring_applied_once_and_only_when_due(ledger):
    ledger.add_recurring("Аренда", "expense", "30000", "Карта", "ЖКХ", 5)
    ledger.add_recurring("Зарплата", "income", "80000", "Карта", "Зарплата", 31)
    assert ledger.apply_recurring("2026-02", today=date(2026, 2, 10)) == ["Аренда"]
    assert ledger.apply_recurring("2026-02", today=date(2026, 2, 28)) == ["Зарплата"]
    assert ledger.apply_recurring("2026-02", today=date(2026, 3, 1)) == []
    dates = sorted(t.date for t in ledger.transactions(month="2026-02"))
    assert dates == ["2026-02-05", "2026-02-28"]  # 31-е в феврале -> последний день


def test_recurring_can_be_reapplied_after_manual_delete(ledger):
    ledger.add_recurring("Аренда", "expense", "30000", "Карта", "ЖКХ", 5)
    ledger.apply_recurring("2026-09", today=date(2026, 9, 30))
    ledger.delete_transaction(ledger.transactions()[0].id)
    assert ledger.apply_recurring("2026-09", today=date(2026, 9, 30)) == ["Аренда"]


def test_csv_roundtrip(ledger, tmp_path):
    ledger.add_income("1000,5", "Карта", "Зарплата", "2026-09-01", "аванс")
    ledger.add_transfer("100", "Карта", "Наличные", "2026-09-02")
    path = str(tmp_path / "out.csv")
    assert ledger.export_csv(path) == 2

    other = Ledger(str(tmp_path / "other.db"))
    assert other.import_csv(path) == 2
    assert other.balances() == {"Карта": 90050, "Наличные": 10000}
    assert other.transactions()[0].note == "аванс"


def test_csv_import_creates_missing_and_is_atomic(ledger, tmp_path):
    path = tmp_path / "in.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "type", "amount", "account", "category", "to_account", "note"])
        w.writerow(["2026-09-01", "expense", "10", "Вклад", "Хобби", "", ""])
        w.writerow(["2026-09-02", "expense", "abc", "Карта", "Продукты", "", ""])
    with pytest.raises(FinanceError, match="строка 3"):
        ledger.import_csv(str(path))
    assert ledger.transactions() == []
    assert "Вклад" not in ledger.balances()
    assert "Хобби" not in [c["name"] for c in ledger.categories()]

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "type", "amount", "account", "category", "to_account", "note"])
        w.writerow(["2026-09-01", "expense", "10", "Вклад", "Хобби", "", ""])
    assert ledger.import_csv(str(path)) == 1
    assert ledger.balances()["Вклад"] == -1000


def test_csv_import_requires_date(ledger, tmp_path):
    path = tmp_path / "in.csv"
    path.write_text("date,type,amount,account,category\n,expense,10,Карта,Продукты\n", encoding="utf-8")
    with pytest.raises(FinanceError, match="дата"):
        ledger.import_csv(str(path))
