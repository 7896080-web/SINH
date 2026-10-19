"""Юнит-экономика: формулы, себестоимость на дату, налог, распределение общих расходов."""
from datetime import date
from decimal import Decimal

from finapp.finance import values as v
from finapp.finance.charts import compact_rub, stacked_columns
from finapp.finance.economics import Filters, platform_summary, trend, unit_economics
from finapp.finance.settings import FinanceSettings
from finapp.models import ProductCost
from tests.finance_factories import make_product, make_report

PERIOD = Filters(date(2026, 9, 1), date(2026, 9, 30))
NO_TAX = FinanceSettings(tax_mode="none")


def _sale(uid="u1", day=date(2026, 9, 5), qty=1, revenue=250000, payout=210000, **kw):
    return {"operation": v.SALE, "uid_1c": uid, "barcode": "2000000000012", "article": "A-1",
            "operation_date": day, "quantity": qty, "revenue": revenue, "payout": payout,
            "commission": revenue - payout, **kw}


def _return(**kw):
    line = _sale(**kw)
    line.update(operation=v.RETURN, revenue=-line["revenue"], payout=-line["payout"],
                commission=-line["commission"])
    return line


def _cost(db, uid, kop, since=date(2000, 1, 1)):
    db.add(ProductCost(uid_1c=uid, valid_from=since, cost_minor=kop, currency="RUB"))
    db.commit()


def test_profit_formula_with_cogs_and_return(db):
    make_product(db)
    _cost(db, "u1", 90000)
    make_report(db, lines=[_sale(), _sale(), _return(),
                           {"operation": v.LOGISTICS, "uid_1c": "u1", "barcode": "2000000000012",
                            "article": "A-1", "quantity": 0, "logistics": 15000}])
    rows, unassigned, total, _ = unit_economics(db, PERIOD, NO_TAX)
    t = rows[0].totals
    assert (t.sold_qty, t.returned_qty, t.net_qty) == (2, 1, 1)
    assert t.revenue == 250000
    assert t.total_payout == 210000 - 15000
    assert t.cogs == 90000                         # вернувшаяся штука вернула и себестоимость
    assert t.profit == 195000 - 90000
    assert round(t.margin, 4) == round(105000 / 195000, 4)      # от к перечислению, не от выручки
    assert round(t.roi, 4) == round(105000 / 90000, 4)
    assert t.profit_per_unit == 105000
    assert total.profit == t.profit and unassigned.total_payout == 0


def test_margin_needs_something_to_be_paid_out(db):
    """Маржинальность — от суммы к перечислению. Логистика съела всё перечисление —
    маржинальности нет (а не «−300 %» от копеечной базы или деление на ноль)."""
    make_product(db)
    _cost(db, "u1", 90000)
    make_report(db, lines=[_sale(),
                           {"operation": v.LOGISTICS, "uid_1c": "u1", "barcode": "2000000000012",
                            "article": "A-1", "quantity": 0, "logistics": 210000}])
    rows, *_ = unit_economics(db, PERIOD, NO_TAX)
    t = rows[0].totals
    assert t.revenue > 0 and t.total_payout == 0
    assert t.margin is None and t.roi is not None


def test_cost_is_taken_on_the_operation_date(db):
    make_product(db)
    _cost(db, "u1", 80000, since=date(2026, 9, 1))
    _cost(db, "u1", 100000, since=date(2026, 9, 15))
    make_report(db, lines=[_sale(day=date(2026, 9, 10)), _sale(day=date(2026, 9, 20))])
    rows, *_ = unit_economics(db, PERIOD, NO_TAX)
    assert rows[0].totals.cogs == 80000 + 100000


def test_sales_before_first_cost_date_use_the_earliest_cost(db):
    """Себестоимость обычно заводят «с сегодняшнего дня» — прошлые продажи не
    должны выглядеть продажами без себестоимости."""
    make_product(db)
    _cost(db, "u1", 70000, since=date(2026, 9, 20))
    make_report(db, lines=[_sale(day=date(2026, 9, 2))])
    rows, *_ = unit_economics(db, PERIOD, NO_TAX)
    assert rows[0].totals.cogs == 70000 and rows[0].totals.cost_missing_qty == 0


def test_missing_cost_is_counted_not_hidden(db):
    make_product(db)
    make_report(db, lines=[_sale(), _sale()])
    rows, *_ = unit_economics(db, PERIOD, NO_TAX)
    assert rows[0].totals.cogs == 0
    assert rows[0].totals.cost_missing_qty == 2


def test_tax_modes_and_extra_cost(db):
    make_product(db)
    _cost(db, "u1", 100000)
    make_report(db, lines=[_sale()])
    revenue_tax = FinanceSettings(tax_mode="revenue", tax_rate=Decimal("6"), extra_cost_kop=5000)
    rows, *_ = unit_economics(db, PERIOD, revenue_tax)
    t = rows[0].totals
    assert t.tax == 15000                       # 6 % от 2500
    assert t.extra == 5000
    assert t.profit == 210000 - 100000 - 5000 - 15000

    profit_tax = FinanceSettings(tax_mode="profit", tax_rate=Decimal("15"), extra_cost_kop=5000)
    rows, *_ = unit_economics(db, PERIOD, profit_tax)
    assert rows[0].totals.tax == 15750          # 15 % от (2100 − 1000 − 50)


def test_usn_income_with_non_refundable_vat_inside_the_price(db):
    """Режим владельца (18.10.2026): УСН «доходы» 5% и невозвратный НДС 5%.
    НДС — внутри цены (из 2500 ₽ — 2500·5/105 = 119,05 ₽), база УСН — выручка
    без НДС. Вместе — 10/105 ≈ 9,52% выручки, а не 10%."""
    from finapp.finance.settings import effective_rate, tax_text
    make_product(db)
    _cost(db, "u1", 100000)
    make_report(db, lines=[_sale()])
    settings = FinanceSettings(tax_mode="revenue", tax_rate=Decimal("5"), vat_rate=Decimal("5"))
    rows, *_ = unit_economics(db, PERIOD, settings)
    t = rows[0].totals
    assert t.vat == 11905                        # 250000 · 5/105 = 11904,76
    assert t.tax == 11905                        # (250000 − 11905) · 5% = 11904,75
    assert t.profit == 210000 - 100000 - 11905 - 11905
    assert round(effective_rate(settings), 2) == Decimal("9.52")
    assert tax_text(settings) == ("УСН «доходы» 5% от реализации без НДС + НДС 5% внутри цены, невозвратный"
                                  " (реализация у WB — «Вайлдберриз реализовал»)")


def test_unassigned_costs_shown_separately_or_allocated_to_the_kopeck(db):
    make_product(db, uid="u1", article="A-1", barcodes=("111",))
    make_product(db, uid="u2", article="B-2", barcodes=("222",))
    make_report(db, lines=[
        _sale(uid="u1", barcode="111", revenue=100000, payout=80000),
        _sale(uid="u2", barcode="222", article="B-2", revenue=200000, payout=160000),
        {"operation": v.STORAGE, "quantity": 0, "storage": 1001},   # без товара
    ])
    rows, unassigned, total, allocated = unit_economics(db, PERIOD, NO_TAX)
    assert not allocated and unassigned.storage == 1001
    assert total.total_payout == 240000 - 1001

    rows, unassigned, total, allocated = unit_economics(db, PERIOD, NO_TAX, allocate=True)
    assert allocated and unassigned.storage == 0
    shares = {g.article: g.totals.storage for g in rows}
    assert sum(shares.values()) == 1001          # ни копейки не потеряно на округлении
    assert shares == {"A-1": 334, "B-2": 667}
    assert total.total_payout == 240000 - 1001


def test_group_by_sku_and_by_article(db):
    make_product(db, uid="u1", article="A-1", size="46", barcodes=("111",))
    make_product(db, uid="u2", article="A-1", size="48", barcodes=("222",))
    make_report(db, lines=[_sale(uid="u1", barcode="111"), _sale(uid="u2", barcode="222")])
    by_article, *_ = unit_economics(db, PERIOD, NO_TAX, group_by="article")
    by_sku, *_ = unit_economics(db, PERIOD, NO_TAX, group_by="sku")
    assert len(by_article) == 1 and by_article[0].totals.sold_qty == 2
    assert sorted(g.size for g in by_sku) == ["46", "48"]


def test_unmatched_line_is_grouped_by_report_article_and_flagged(db):
    make_report(db, lines=[_sale(uid=None, barcode="999", article="WB-ART")])
    rows, *_ = unit_economics(db, PERIOD, NO_TAX)
    assert rows[0].article == "WB-ART"
    assert rows[0].matched is False
    assert rows[0].totals.unmatched_lines == 1


def test_period_and_platform_filters(db):
    make_report(db, platform="wb", lines=[_sale(day=date(2026, 9, 5))])
    make_report(db, platform="lamoda", lines=[_sale(day=date(2026, 9, 6), revenue=300000,
                                                     payout=200000)])
    make_report(db, platform="kit", period=(date(2026, 10, 1), date(2026, 10, 7)),
                lines=[_sale(day=date(2026, 10, 2))])
    summary = platform_summary(db, PERIOD, NO_TAX)
    assert set(summary) == {"wb", "lamoda"}
    assert summary["lamoda"].revenue == 300000
    rows, *_ = unit_economics(db, Filters(PERIOD.date_from, PERIOD.date_to, "wb"), NO_TAX)
    assert rows[0].totals.revenue == 250000


def test_trend_buckets_by_week_and_chart_geometry(db):
    make_report(db, lines=[_sale(day=date(2026, 9, 1)), _sale(day=date(2026, 9, 3)),
                           _sale(day=date(2026, 9, 9))])
    rows = trend(db, PERIOD, NO_TAX, "week")
    assert [(start, t.revenue) for start, _, t in rows] == [(date(2026, 8, 31), 500000),
                                                           (date(2026, 9, 7), 250000)]
    chart = stacked_columns([(s, p, t.revenue) for s, p, t in rows], ["wb"], "week")
    assert len(chart["columns"]) == 2
    tallest = chart["columns"][0].segments[0]
    assert tallest.h > chart["columns"][1].segments[0].h
    assert tallest.w <= 24                      # толщина столбика не больше 24px


def test_negative_week_is_not_stacked_but_kept_in_values():
    chart = stacked_columns([(date(2026, 9, 7), "wb", -1000), (date(2026, 9, 7), "kit", 5000)],
                            ["wb", "kit"], "week")
    column = chart["columns"][0]
    assert [s.platform for s in column.segments] == ["kit"]
    assert column.values == {"wb": -1000, "kit": 5000}


def test_axis_labels_are_compact():
    assert compact_rub(1_240_000) == "1,2 млн"
    assert compact_rub(2_000_000) == "2 млн"
    assert compact_rub(500_000) == "500 тыс"
    assert compact_rub(0) == "0"
