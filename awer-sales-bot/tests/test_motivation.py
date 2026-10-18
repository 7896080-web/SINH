"""Мотивация и обучение: рекорды, цель недели, «кого похвалить», цель магазина, уроки, новинки, листок старшего,
программа новичка, квизы."""
from datetime import date, timedelta
from pathlib import Path

import pytest

from awer_sales_bot import achievements
from awer_sales_bot.importers.onec_sales import import_file
from awer_sales_bot.messages import daily
from awer_sales_bot.planning import service
from test_flow import env  # noqa: F401 — фикстура: история из обезличенных ведомостей

HEAD = "Дата;Время;Склад;Документ;Чек;Продавец;Номенклатура;Номенклатурная группа;Количество;Цена;Сумма;Скидка"


def write_sales(path: Path, rows: list[tuple]) -> Path:
    """rows: (день, продавец, сумма, вещей[, категория, чек, скидка]) — одна строка на вещь не нужна, хватит итога."""
    out = [HEAD]
    for i, r in enumerate(rows):
        d, seller, amount, qty = r[:4]
        cat = r[4] if len(r) > 4 else "Джинсы"
        rec = r[5] if len(r) > 5 else ""
        disc = r[6] if len(r) > 6 else 0
        price = (amount + disc) / qty if qty else amount
        out.append(f"{d:%d.%m.%Y};{'12:00' if rec else ''};ГОРОД_склад;Д{d:%m%d};{rec};{seller};Арт{i};{cat};{qty};"
                   f"{price:.2f};{amount:.2f};{disc}")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def oct_day(n: int) -> date:
    return date(2026, 10, 1) + timedelta(days=n - 1)


def test_records_and_streak(env, tmp_path):  # noqa: F811
    cfg, db = env
    service.freeze(db, cfg, "2026-10")
    sid = db.seller_id("Продавец04", create=False)
    rows = [(oct_day(i), "Продавец04", 30_000, 3) for i in range(1, 12)]  # 11 смен по 30 000
    rows += [(oct_day(12), "Продавец04", 40_000, 4), (oct_day(13), "Продавец04", 45_000, 5),
             (oct_day(14), "Продавец04", 50_000, 6)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    plan = service.frozen_plan(db, "2026-10", sid)  # 561 764 → норма смены ≈ 37 451
    got = {a.kind: a for a in achievements.for_day(db, cfg, sid, oct_day(14), plan)}
    assert "Личный рекорд: 50 000 ₽" in got["record_sales"].text and "45 000 ₽" in got["record_sales"].text
    assert got["record_items"].notable
    assert got["streak"].text.startswith("Серия: 3 смены подряд") and got["streak"].notable
    assert achievements.for_day(db, cfg, sid, oct_day(15), plan) == []  # не работал — нечего хвалить
    text = daily(db, cfg, sid, oct_day(14))
    assert "🏅 Личный рекорд: 50 000 ₽" in text


def test_best_shift_of_month_before_enough_history(env, tmp_path):  # noqa: F811
    cfg, db = env
    sid = db.seller_id("Продавец04", create=False)
    rows = [(oct_day(1), "Продавец04", 20_000, 2), (oct_day(2), "Продавец04", 25_000, 2),
            (oct_day(3), "Продавец04", 30_000, 3)]
    import_file(write_sales(tmp_path / "s.csv", rows), db, cfg)
    kinds = [a.kind for a in achievements.for_day(db, cfg, sid, oct_day(3), None)]
    assert kinds == ["best_month"]  # «за всё время» — только после 10 смен в истории
