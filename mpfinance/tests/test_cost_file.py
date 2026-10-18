"""Файл себестоимости из 1С — строго `uid|цена`, и одна неверная цифра не
переворачивает итоги.

18.10.2026 на бою справочник баркодов (`uid|артикул|наименование|баркод|размер|
цвет`) загрузили как себестоимость: разбор взял артикул «70534 10146» за цену
$7 053 410 146, и дашборд показал себестоимость WB 692 млрд ₽."""
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from finapp.finance import costs, values as v
from finapp.finance.economics import Totals, add_line
from finapp.models import OnecTask, Product, ProductCost
from tests.finance_factories import make_product

ROOT = Path(__file__).resolve().parents[1]
DICT = ("66fab70a-d927-11ed-9119-901b0edac6ce|70534 10146|70534 10146 АВЕР Рубашка К/р Белый|2000931955538|L|Белый\n"
        "9cf16164-e399-11e4-8215-005056c00008|1102  mw|1102  mw Max Way Футболка Л 14|2000000007557|U|4\n"
        "29e66324-f16d-11e4-80cc-448a5b2c2cf2|1532|1532 Кежл Брюки Л 14|2000000096155|31|\n")


def test_cost_file_takes_uid_and_price_only():
    parsed, bad = costs.parse_cost_file("﻿u1|12.50\nu2|7,3\nu3|4\n")
    assert parsed == {"u1": 1250, "u2": 730, "u3": 400} and bad == 0


def test_barcode_dictionary_is_not_a_cost_file():
    with pytest.raises(costs.CostFileError, match="похоже на справочник баркодов"):
        costs.parse_cost_file(DICT)


def test_spaces_inside_the_price_are_not_squeezed_into_a_number():
    """Пробел внутри — признак, что во втором поле не цена: «70534 10146» больше
    не становится 7053410146."""
    with pytest.raises(costs.CostFileError):
        costs.parse_cost_file("u1|70534 10146\n")


def test_a_few_broken_lines_in_a_large_file_are_counted_not_fatal():
    good = "".join(f"u{i}|{i % 50 + 1}.25\n" for i in range(100))
    parsed, bad = costs.parse_cost_file(good + "u-bad|\nu-zero|0\n")
    assert len(parsed) == 100 and bad == 2


def test_dictionary_uploaded_as_cost_is_refused_and_nothing_is_written(client, db):
    make_product(db, uid="66fab70a-d927-11ed-9119-901b0edac6ce", article="70534 10146")
    response = client.post("/diagnostics/upload", data={"kind": "cost"},
                           files={"file": ("barcodes_price_20261012092230721504.txt", DICT.encode("utf-8"))},
                           follow_redirects=True)
    assert "Файл не принят" in response.text and "справочник баркодов" in response.text
    assert db.query(ProductCost).count() == 0


def test_upload_without_choosing_the_kind_is_refused(client, db):
    response = client.post("/diagnostics/upload", data={"kind": ""},
                           files={"file": ("x.txt", b"u1|1.00\n")}, follow_redirects=True)
    assert "Выберите, что за файл" in response.text


def test_1c_answer_with_a_foreign_cost_file_fails_the_task(db):
    from finapp import onec
    make_product(db, uid="66fab70a-d927-11ed-9119-901b0edac6ce", article="70534 10146")
    task = OnecTask(command="EXPORT_COST_PRICES", key="FINCOST-1", line="EXPORT_COST_PRICES|FINCOST-1",
                    status="sent")
    db.add(task)
    db.commit()
    onec.apply_result_text(db, "FINCOST-1|OK|себестоимость выгружена|EXPORT_COST_PRICES", cost_text=DICT)
    db.commit()
    assert task.status == "failed" and "себестоимость не принята" in task.result_detail
    assert db.query(ProductCost).count() == 0


def _sale(revenue, qty=1):
    return ("wb", 1, v.SALE, date(2026, 9, 8), "u1", "b", "a", "n", "s", qty, revenue, 0, revenue,
            0, 0, 0, 0, 0, 0)


def test_cost_ten_times_the_price_is_not_taken_as_a_loss():
    totals = Totals()
    add_line(totals, _sale(460000), 59315934511200)           # тот самый случай: 593 млрд ₽ за штуку
    add_line(totals, _sale(460000), 150000)                   # обычная себестоимость
    assert totals.cogs == 150000
    assert (totals.cost_suspicious_qty, totals.cost_missing_qty) == (1, 1)
    edge = Totals()
    add_line(edge, _sale(100000, qty=2), 500000)              # ровно в 10 раз — ещё правда
    assert edge.cogs == 1000000 and edge.cost_suspicious_qty == 0


def test_dashboard_marks_the_suspicious_cost(client, db):
    from tests.finance_factories import make_report
    make_product(db, uid="u1", barcodes=("2000000000012",))
    db.add(ProductCost(uid_1c="u1", valid_from=date(2000, 1, 1), cost_minor=59315934511200, currency="RUB",
                       source="excel", updated_by="admin"))
    db.commit()
    report = make_report(db, key="SR-wb-susp", lines=[{"operation": v.SALE, "quantity": 1, "revenue": 460000,
                                                       "payout": 400000, "barcode": "2000000000012"}])
    from finapp.finance.importer import resolve_uids
    resolve_uids(db, report)
    db.commit()
    page = client.get(f"/finance?date_from={report.period_from}&date_to={report.period_to}").text
    assert "себестоимость под сомнением" in page and "593" not in page.split("Куда уходит выручка")[1][:3000]


# ------------------------------------------------------------ миграция 0005

def _alembic(url, *args):
    env = dict(os.environ, DATABASE_URL=url, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(ROOT), env=env,
                          capture_output=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stdout + done.stderr


def test_migration_removes_only_costs_that_are_an_article(tmp_path):
    path = tmp_path / "m.db"
    url = f"sqlite:///{path}"
    _alembic(url, "upgrade", "0004")
    conn = sqlite3.connect(path)
    for uid, article in (("u1", "70534 10146"), ("u2", "36192"), ("u3", "55045 6282.1"), ("u4", "39615MAVIMEL")):
        conn.execute("INSERT INTO products (uid_1c, article, name) VALUES (?, ?, ?)", (uid, article, "т"))
    rows = [("u1", 705341014600, "1c"),        # артикул как цена — удалить
            ("u2", 3619200, "1c"),             # то же
            ("u3", 55045628210, "1c"),         # то же, артикул с точкой
            ("u4", 1250, "1c"),                # настоящая цена — оставить
            ("u2", 3619200, "excel")]          # ручную не трогаем никогда
    for i, (uid, cost, source) in enumerate(rows):
        conn.execute("INSERT INTO product_costs (uid_1c, valid_from, cost_minor, currency, source, updated_by) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (uid, f"2026-10-{10 + i}", cost, "USD", source, "t"))
    conn.commit()
    conn.close()
    _alembic(url, "upgrade", "head")
    conn = sqlite3.connect(path)
    left = sorted(conn.execute("SELECT uid_1c, cost_minor, source FROM product_costs").fetchall())
    logged = conn.execute("SELECT details FROM audit_log WHERE action = 'product_costs_cleanup'").fetchone()
    conn.close()
    assert left == [("u2", 3619200, "excel"), ("u4", 1250, "1c")]
    assert logged and "3 записей" in logged[0]
