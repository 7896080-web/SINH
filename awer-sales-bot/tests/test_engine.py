"""Регрессия: движок обязан давать те же цифры, что согласованная Excel-модель
(«План_и_ставка_продавцов.xlsx», 18.10.2026). История — обезличенная выгрузка
из ведомостей ЗП июль 2025 – сентябрь 2026.
"""
import json
from pathlib import Path

import pytest

from awer_sales_bot.planning import History, Params, evaluate, month_add, seller_plan, steps_for, store_plan, threshold_amounts

FIX = Path(__file__).parent / "fixtures" / "history_anon.json"


@pytest.fixture(scope="module")
def hist() -> History:
    d = json.loads(FIX.read_text(encoding="utf-8"))
    h = History(roles=d["roles"], coverage=d["coverage"])
    for m, sellers in d["months"].items():
        for s, v in sellers.items():
            h.add(m, s, v["sales"], v["fte"])
    return h


def test_month_add():
    assert month_add("2026-01", -1) == "2025-12"
    assert month_add("2026-10", -15) == "2025-07"
    assert month_add("2025-12", 1) == "2026-01"


def test_store_plan_october(hist):
    sp = store_plan(hist, "2026-10")
    assert sp.last_year == pytest.approx(6_963_861, abs=1)
    assert sp.seasonality == pytest.approx(0.924, abs=0.001)
    assert sp.plan == pytest.approx(5_605_452.19, abs=1)
    assert sp.coef == pytest.approx(1.083, abs=0.001)
    assert sp.newcomer_plan == pytest.approx(449_146.09, abs=1)


@pytest.mark.parametrize("seller,expected", [
    ("Админ2", 1_107_673),  # Эльвина
    ("Админ1", 941_654),  # Юлия
    ("Продавец04", 561_764),
    ("Продавец08", 706_013),
    ("Продавец17", 663_213),
])
def test_personal_plans_october(hist, seller, expected):
    assert seller_plan(hist, "2026-10", seller).plan_full == pytest.approx(expected, abs=1)


def test_newcomer_plan(hist):
    sp = seller_plan(hist, "2026-10", "Новенькая")
    assert sp.method == "newcomer"
    assert sp.plan_full == pytest.approx(449_146.09, abs=1)


def test_backtest_august_admin(hist):
    plan = seller_plan(hist, "2026-08", "Админ2").plan_full
    assert plan == pytest.approx(960_791, abs=1)
    x = hist.get("2026-08", "Админ2")
    r = evaluate(x.sales, x.fte, plan, "admin")
    assert r.performance == pytest.approx(1.37, abs=0.01)
    assert r.rate == 53_000  # 25 000 + 3 шага, + 13 000 доплата


def test_backtest_september_no_bonus(hist):
    plan = seller_plan(hist, "2026-09", "Админ2").plan_full
    assert plan == pytest.approx(1_275_323, abs=1)
    x = hist.get("2026-09", "Админ2")
    r = evaluate(x.sales, x.fte, plan, "admin")
    assert r.steps == 0 and r.rate == 38_000


def test_steps_edges():
    assert steps_for(0.95) == 0
    assert steps_for(1.0999) == 0
    assert steps_for(1.10) == 1  # ровно +10 % — шаг засчитан
    assert steps_for(1.2) == 2
    assert steps_for(5.0) == 40  # ставку ограничивает max_rate, не steps_for


def test_rate_cap_and_stabilizer():
    p = Params(stabilizer="one_step")
    r = evaluate(2_000_000, 1, 1_000_000, "seller", p, prev_steps=0)
    assert r.steps == 1 and r.rate == 30_000
    r = evaluate(2_000_000, 1, 1_000_000, "seller", Params())
    assert r.rate == 45_000


def test_thresholds():
    t = threshold_amounts(500_000)
    assert t[0] == (500_000, 25_000)
    assert t[1][0] == pytest.approx(550_000) and t[1][1] == 30_000
    assert t[-1][1] == 45_000


def test_manual_trend_and_plan(hist):
    sp = store_plan(hist, "2026-10", trend_override=0.9)
    assert sp.trend == 0.9 and sp.trend_calc == pytest.approx(0.805, abs=0.001)
    assert sp.plan == pytest.approx(6_963_861 * 0.9, abs=1)
    assert seller_plan(hist, "2026-10", "Админ2", manual=1_000_000).plan_full == 1_000_000
