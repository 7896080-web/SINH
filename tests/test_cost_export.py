"""Себестоимость из 1С (команда EXPORT_COST_PRICES, файл cost_*.txt) — база репрайсера.

Закреплено: запрос уходит строкой команды; разбор устойчив к запятой и мусору;
нулевая себестоимость не применяется; товар, пропавший из файла, сохраняет
прежнюю себестоимость; остатки и цены на площадках эта выгрузка не трогает.
"""
from decimal import Decimal

from app.models import DispatchQueueItem, PriceChange, Product
from app.workers.ftp_channel import (COST_EXPORT_COMMAND, LocalExchange, apply_cost_export_files,
                                     build_task_batch, parse_cost_export)


def _exchange(tmp_path) -> LocalExchange:
    exchange = LocalExchange(tmp_path / "t", tmp_path / "r", tmp_path / "a")
    exchange._ensure_dirs()
    return exchange


def test_cost_request_goes_into_task_file(db, tmp_path):
    _, content = build_task_batch(db, request_cost_export=True, exchange=_exchange(tmp_path))
    assert content.splitlines() == [COST_EXPORT_COMMAND]


def test_no_request_no_line(db, tmp_path):
    _, content = build_task_batch(db, request_stock_export=True, exchange=_exchange(tmp_path))
    assert COST_EXPORT_COMMAND not in content


def test_parse_cost_export():
    content = "u1|512.50\nu2|1 000,4\nu3|0\nu4|abc\nu5|-3\n|7\nu6\nu7|NaN\n"
    assert parse_cost_export(content) == {"u1": Decimal("512.50"), "u2": Decimal("1000.40")}


def test_apply_updates_cost_and_touches_nothing_else(db, tmp_path):
    exchange = _exchange(tmp_path)
    db.add(Product(uid_1c="u1", article="A", stock_on_hand=5))
    db.add(Product(uid_1c="u2", article="B", cost_price=Decimal("300")))
    db.commit()
    (exchange.dir_results / "cost_20261001100000.txt").write_text("﻿u1|512.5\nx9|10\n", encoding="utf-8")

    stats = apply_cost_export_files(db, exchange)

    assert stats == {"files": 1, "updated": 1, "unknown": 1}
    u1 = db.get(Product, "u1")
    assert u1.cost_price == Decimal("512.50") and u1.cost_price_updated_at is not None
    assert u1.stock_on_hand == 5
    assert db.get(Product, "u2").cost_price == Decimal("300")    # нет в файле — не обнулили
    assert db.query(DispatchQueueItem).count() == 0
    assert db.query(PriceChange).count() == 0
    assert list(exchange.dir_results.glob("cost_*")) == []              # файл в архиве


def test_newest_file_wins(db, tmp_path):
    exchange = _exchange(tmp_path)
    db.add(Product(uid_1c="u1", article="A"))
    db.commit()
    (exchange.dir_results / "cost_20261001100000.txt").write_text("u1|100", encoding="utf-8")
    (exchange.dir_results / "cost_20261001110000.txt").write_text("u1|200", encoding="utf-8")

    apply_cost_export_files(db, exchange)

    assert db.get(Product, "u1").cost_price == Decimal("200")
