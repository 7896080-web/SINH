"""Обмен с 1С: только свои команды, файлы task_price_*, проверка версии
обработки первым запросом себестоимости, разбор ответа с файлом рядом."""
from decimal import Decimal

import pytest

from priceapp import exchange, onec, settings
from priceapp.models import OnecBarcode, OnecCost, OnecTask
from priceapp.timeutils import now_utc


def _ex(cfg):
    return exchange.LocalExchange(cfg.ONEC_TASKS_DIR, cfg.ONEC_RESULTS_DIR, cfg.ONEC_ARCHIVE_DIR)


def _answer(cfg, label, result, cost=None, dict_=None):
    if cost is not None:
        (cfg.ONEC_RESULTS_DIR / f"cost_{label}.txt").write_text(cost, encoding="utf-8")
    if dict_ is not None:
        (cfg.ONEC_RESULTS_DIR / f"barcodes_{label}.txt").write_text(dict_, encoding="utf-8")
    (cfg.ONEC_RESULTS_DIR / f"result_{label}.txt").write_text(result, encoding="utf-8")


def test_before_ready_only_the_probe_leaves(db, exchange_dirs):
    with pytest.raises(onec.OnecError):
        onec.enqueue_dict(db)
    with pytest.raises(onec.OnecError):
        onec.enqueue_ping(db)
    t = onec.enqueue_cost(db)
    db.commit()
    assert onec.publish_pending(db, _ex(exchange_dirs)) == 1
    files = list(exchange_dirs.ONEC_TASKS_DIR.iterdir())
    assert len(files) == 1 and files[0].name.startswith("task_price_")
    assert files[0].read_text(encoding="utf-8") == f"EXPORT_COST_PRICES|{t.order_id}"


def test_same_command_is_not_queued_twice(db):
    a, b = onec.enqueue_cost(db), onec.enqueue_cost(db)
    assert a.id == b.id


def test_cost_answer_marks_ready_and_loads_costs(db, exchange_dirs):
    t = onec.enqueue_cost(db)
    db.commit()
    ex = _ex(exchange_dirs)
    onec.publish_pending(db, ex)
    label = t.filename[len("task_"):-len(".txt")]
    _answer(exchange_dirs, label, f"{t.order_id}|OK|себестоимость выгружена|EXPORT_COST_PRICES",
            cost="﻿u1|16.24\nu2|9,5\nu3|0\n")
    got = onec.collect_results(db, ex)
    assert got["files"] == 1 and got["ok"] == 1
    db.refresh(t)
    assert t.status == "done" and "строк 2" in t.result_detail
    assert onec.epf_ready(db)
    assert {c.item_id: c.cost_usd for c in db.query(OnecCost)} == {"u1": Decimal("16.24"), "u2": Decimal("9.50")}
    assert list(exchange_dirs.ONEC_RESULTS_DIR.iterdir()) == []          # всё в архиве
    assert {p.name for p in exchange_dirs.ONEC_ARCHIVE_DIR.iterdir()} == {f"result_{label}.txt", f"cost_{label}.txt"}


def test_missing_from_file_keeps_old_cost(db):
    db.add(OnecCost(item_id="u9", cost_usd=Decimal("5")))
    db.commit()
    onec.load_costs(db, {"u1": Decimal("1")})
    db.commit()
    assert db.get(OnecCost, "u9").cost_usd == Decimal("5")


def test_empty_cost_file_fails_task_not_wipes(db, exchange_dirs):
    t = onec.enqueue_cost(db)
    db.commit()
    ex = _ex(exchange_dirs)
    onec.publish_pending(db, ex)
    label = t.filename[5:-4]
    _answer(exchange_dirs, label, f"{t.order_id}|OK|x|EXPORT_COST_PRICES", cost="")
    onec.collect_results(db, ex)
    db.refresh(t)
    assert t.status == "failed" and "не принята" in t.result_detail


def test_dict_after_ready(db, exchange_dirs):
    settings.put(db, settings.EPF_READY_AT, now_utc().isoformat())
    t = onec.enqueue_dict(db)
    db.commit()
    ex = _ex(exchange_dirs)
    onec.publish_pending(db, ex)
    label = t.filename[5:-4]
    _answer(exchange_dirs, label, f"{t.order_id}|OK|справочник выгружен|BARCODE_DICT",
            dict_="u1|39681|Свитшот|b1|L|GRI\nu1|39681|Свитшот|b1b|L|GRI\n")
    onec.collect_results(db, ex)
    assert db.query(OnecBarcode).count() == 2


def test_timeout_of_probe_says_update_module(db, exchange_dirs):
    from datetime import timedelta
    t = onec.enqueue_cost(db)
    db.commit()
    onec.publish_pending(db, _ex(exchange_dirs))
    t.sent_at = now_utc() - timedelta(hours=1)
    db.commit()
    assert onec.mark_timeouts(db) == 1
    db.refresh(t)
    assert t.status == "timeout" and "mark-3" in t.result_detail


def test_unmatched_answer_counted(db, exchange_dirs):
    _answer(exchange_dirs, "price_1", "cost-99|OK|x|EXPORT_COST_PRICES")
    got = onec.collect_results(db, _ex(exchange_dirs))
    assert got["unmatched"] == 1


def test_no_work_no_server(db, monkeypatch):
    def boom():
        raise AssertionError("сервер трогать нельзя")
    monkeypatch.setattr(exchange, "current", boom)
    assert onec.exchange_once(db)["sent"] == 0
