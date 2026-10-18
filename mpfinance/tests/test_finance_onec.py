"""Обмен с 1С своими файлами task_fin_*: проверка версии, отчёт о продажах,
справочник, себестоимость; баркод для 1С по мэппингу Sync Admin.

Граница безопасности: в 1С пишется только то, что оператор явно отправил.
Импорт отчёта сам по себе ничего не отправляет, отчёт с несопоставленными
товарами не уходит вовсе, а пока обработка не подтвердила версию mark-4, не
уходит ничего, кроме FIN_PING (ответ старой обработки лёг бы в общую папку
results, и его унёс бы sync_admin).
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from finapp import config, onec, settings
from finapp.finance import values as v
from finapp.finance.sales_1c import build_plan, detect_timed_out, request_upload
from finapp.finance.settings import DEFAULT_WAREHOUSES, save_setting
from finapp.models import Barcode, OnecTask, OneCUploadStatus, Product, ProductCost
from finapp.timeutils import now_utc, today_local
from tests.finance_factories import make_product, make_report

PERIOD = (date(2026, 9, 1), date(2026, 9, 7))


def _report(db, platform="wb", key="SR-wb-100", lines=None):
    make_product(db, uid="u1", barcodes=("111",))
    make_product(db, uid="u2", article="B-2", barcodes=("222",))
    lines = lines if lines is not None else [
        {"operation": v.SALE, "uid_1c": "u1", "barcode": "111", "quantity": 1, "revenue": 250000,
         "payout": 210000},
        {"operation": v.SALE, "uid_1c": "u1", "barcode": "111", "quantity": 2, "revenue": 480050,
         "payout": 400040},
        {"operation": v.RETURN, "uid_1c": "u2", "barcode": "222", "quantity": 1, "revenue": -199900,
         "payout": -170000},
        {"operation": v.LOGISTICS, "uid_1c": "u1", "barcode": "111", "quantity": 0, "logistics": 9000},
        # корректировка суммы без количества — в товарный документ не кладётся
        {"operation": v.SALE, "uid_1c": "u1", "barcode": "111", "quantity": 0, "revenue": 1500,
         "payout": 1200},
    ]
    return make_report(db, platform=platform, period=PERIOD, key=key, lines=lines)


def _tasks(cfg) -> list[list[str]]:
    return [p.read_text(encoding="utf-8").splitlines() for p in sorted(Path(cfg.ONEC_TASKS_DIR).glob("task_fin_*.txt"))]


def _answer(cfg, label: str, result: str, cost: str | None = None, barcodes: str | None = None):
    results = Path(cfg.ONEC_RESULTS_DIR)
    if cost is not None:
        (results / f"cost_{label}.txt").write_text(cost, encoding="utf-8")
    if barcodes is not None:
        (results / f"barcodes_{label}.txt").write_text(barcodes, encoding="utf-8")
    (results / f"result_{label}.txt").write_text(result, encoding="utf-8-sig")


# ------------------------------------------------------------ версия обработки

def test_nothing_but_fin_ping_goes_before_mark4(db, exchange_dirs):
    with pytest.raises(onec.OnecError, match="mark-4"):
        onec.enqueue(db, "EXPORT_COST_PRICES")
    report = _report(db)
    assert any("mark-4" in p for p in request_upload(db, report, "admin"))

    onec.enqueue(db, onec.PROBE)
    db.commit()
    onec.exchange_once(db)
    files = _tasks(exchange_dirs)
    assert len(files) == 1 and files[0][0].startswith("FIN_PING|FINPING-")


def test_fin_ping_answer_unlocks_the_other_commands(db, exchange_dirs):
    task = onec.enqueue(db, onec.PROBE)
    db.commit()
    onec.exchange_once(db)
    _answer(exchange_dirs, "fin_1", f"{task.key}|OK|mark-4|FIN_PING")
    onec.exchange_once(db)
    assert onec.epf_ready(db)
    assert db.get(OnecTask, task.id).status == "done"
    # Ответ разобран и унесён в СВОЙ архив.
    assert not list(Path(exchange_dirs.ONEC_RESULTS_DIR).glob("result_*"))
    assert (Path(exchange_dirs.ONEC_ARCHIVE_DIR) / "result_fin_1.txt").exists()
    onec.enqueue(db, "BARCODE_DICT")


def test_old_module_version_is_not_accepted(db, exchange_dirs):
    task = onec.enqueue(db, onec.PROBE)
    db.commit()
    onec.exchange_once(db)
    _answer(exchange_dirs, "fin_1", f"{task.key}|OK|mark-3|FIN_PING")
    onec.exchange_once(db)
    assert not onec.epf_ready(db)
    assert db.get(OnecTask, task.id).status == "failed"


def test_silent_old_module_times_out_with_a_hint(db, exchange_dirs):
    task = onec.enqueue(db, onec.PROBE)
    db.commit()
    onec.exchange_once(db)
    task.sent_at = now_utc() - timedelta(minutes=config.ONEC_TIMEOUT_MINUTES + 1)
    db.commit()
    onec.exchange_once(db)
    db.refresh(task)
    assert task.status == "timeout" and "mark-4" in task.result_detail


# ------------------------------------------------------------ отчёт о продажах

def test_plan_sends_payout_to_seller_aggregated_by_barcode(db, epf_ready):
    """В 1С — баркод, количество и СУММА К ПЕРЕЧИСЛЕНИЮ ПРОДАВЦУ (не выручка)."""
    plan = build_plan(db, _report(db))
    assert [(l.kind, l.barcode, l.quantity, l.amount_kop) for l in plan.lines] == [
        ("RETURN", "222", 1, 170000), ("SALE", "111", 3, 610040)]
    assert (plan.skipped_lines, plan.skipped_amount_kop) == (1, 1200)


def test_imported_report_is_not_sent_without_the_button(db, epf_ready, exchange_dirs):
    _report(db)
    onec.exchange_once(db)
    assert _tasks(exchange_dirs) == []


def test_requested_report_goes_alone_into_its_own_task_file(db, epf_ready, exchange_dirs):
    report = _report(db)
    onec.enqueue(db, "EXPORT_COST_PRICES")
    assert request_upload(db, report, "admin") == []
    onec.exchange_once(db)
    files = _tasks(exchange_dirs)
    assert len(files) == 2
    sales = next(f for f in files if f[0].startswith("SALES_REPORT"))
    other = next(f for f in files if not f[0].startswith("SALES_REPORT"))
    assert sales == [
        "SALES_REPORT|SR-wb-100|wb|Wildberries_Склад_FBO|20260907|RETURN|222|1|1700.00",
        "SALES_REPORT|SR-wb-100|wb|Wildberries_Склад_FBO|20260907|SALE|111|3|6100.40",
    ]
    assert other[0].startswith("EXPORT_COST_PRICES|FINCOST-")
    db.refresh(report)
    assert report.onec_status == OneCUploadStatus.sent and report.onec_lines_sent == 2
    onec.exchange_once(db)                         # второй проход ничего не повторяет
    assert len(_tasks(exchange_dirs)) == 2


def test_report_with_unmatched_goods_is_refused(db, epf_ready):
    report = _report(db, lines=[{"operation": v.SALE, "uid_1c": None, "barcode": "999",
                                 "quantity": 1, "revenue": 1000, "payout": 800}])
    problems = request_upload(db, report, "admin")
    assert any("без товара 1С" in p for p in problems)
    assert report.onec_status == OneCUploadStatus.none


def test_lamoda_needs_a_warehouse_before_sending(db, epf_ready, exchange_dirs):
    report = _report(db, platform="lamoda", key="SR-lamoda-1")
    assert DEFAULT_WAREHOUSES["lamoda"] == ""
    assert any("склад" in p for p in request_upload(db, report, "admin"))
    save_setting(db, "FIN_WAREHOUSE_LAMODA", "Lamoda_Склад")
    db.commit()
    assert request_upload(db, report, "admin") == []
    onec.exchange_once(db)
    assert "|lamoda|Lamoda_Склад|" in _tasks(exchange_dirs)[0][0]


def test_report_broken_between_click_and_send_is_failed_not_sent(db, epf_ready, exchange_dirs):
    report = _report(db)
    request_upload(db, report, "admin")
    save_setting(db, "FIN_WAREHOUSE_WB", "")
    db.commit()
    onec.exchange_once(db)
    assert _tasks(exchange_dirs) == []
    db.refresh(report)
    assert report.onec_status == OneCUploadStatus.failed and "склад" in report.onec_detail


def test_result_ok_error_unknown_and_late(db, epf_ready, exchange_dirs):
    report = _report(db)
    request_upload(db, report, "admin")
    onec.exchange_once(db)
    stats = onec.apply_result_text(db, "SR-wb-404|OK|№1|SALES_REPORT")
    assert stats["unmatched"] == 1

    report.onec_sent_at = now_utc() - timedelta(minutes=config.ONEC_TIMEOUT_MINUTES + 1)
    db.commit()
    assert [r.id for r in detect_timed_out(db)] == [report.id]
    # Опоздавший ответ принимается: документы в 1С созданы.
    _answer(exchange_dirs, "fin_9", "SR-wb-100|OK|продажи №15, возвраты №3|SALES_REPORT")
    onec.exchange_once(db)
    db.refresh(report)
    assert report.onec_status == OneCUploadStatus.done
    assert report.onec_detail == "продажи №15, возвраты №3"


def test_result_error_marks_report_failed(db, epf_ready, exchange_dirs):
    report = _report(db)
    request_upload(db, report, "admin")
    onec.exchange_once(db)
    onec.apply_result_text(db, "SR-wb-100|ERROR|Документ №15 уже проведён|SALES_REPORT")
    assert report.onec_status == OneCUploadStatus.failed and "проведён" in report.onec_detail


# ------------------------------------------------------------ баркод для 1С

def test_report_barcode_unknown_to_1c_is_replaced_by_the_1c_barcode(db, epf_ready, sync_db):
    """Баркод площадки привязан к товару в «Мэппинге» Sync Admin, но в 1С его
    нет — в 1С уходит баркод того же товара, прописанный в самой 1С."""
    from finapp.finance.importer import resolve_uids
    make_product(db, uid="u1", barcodes=("2000000000012",))     # справочник 1С: баркод 1С
    sync_db.barcode("2000000000012", "u1", "1c_dict")
    sync_db.barcode("4600000000001", "u1", "excel_import")       # баркод площадки, привязан руками
    report = make_report(db, period=PERIOD, key="SR-wb-1", lines=[
        {"operation": v.SALE, "barcode": "4600000000001", "quantity": 2, "revenue": 5000,
         "payout": 4000}])
    assert resolve_uids(db, report) == 0
    plan = build_plan(db, report)
    assert [(l.barcode, l.report_barcodes, l.amount_kop) for l in plan.lines] == [
        ("2000000000012", ["4600000000001"], 4000)]


def test_sync_admin_mapping_wins_over_own_dictionary(db, sync_db):
    from finapp.finance.importer import resolve_uids
    make_product(db, uid="u1", barcodes=("777",))
    sync_db.product("u2", article="B-2")
    sync_db.barcode("777", "u2", "excel_repoint")                # человек перевесил баркод
    report = make_report(db, period=PERIOD, key="SR-wb-2", lines=[
        {"operation": v.SALE, "barcode": "777", "quantity": 1, "revenue": 1000, "payout": 800}])
    resolve_uids(db, report)
    assert report.lines[0].uid_1c == "u2"
    # Товар из Sync Admin заведён в наш справочник — на него сошлются себестоимость и юнит-экономика.
    assert db.get(Product, "u2").article == "B-2"


def test_without_sync_admin_the_1c_dictionary_is_used(db):
    from finapp.finance.importer import resolve_uids
    make_product(db, uid="u1", barcodes=("777",))
    report = make_report(db, period=PERIOD, key="SR-wb-3", lines=[
        {"operation": v.SALE, "barcode": "777", "quantity": 1, "revenue": 1000, "payout": 800}])
    notes = []
    assert resolve_uids(db, report, notes) == 0
    assert any("не прочитан" in n for n in notes)


def test_goods_without_any_1c_barcode_are_not_sent(db, epf_ready, sync_db):
    from finapp.finance.importer import resolve_uids
    sync_db.product("u3")
    sync_db.barcode("555", "u3", "pool_match")                   # только догадка по площадке
    report = make_report(db, period=PERIOD, key="SR-wb-4", lines=[
        {"operation": v.SALE, "barcode": "555", "quantity": 1, "revenue": 1000, "payout": 800}])
    resolve_uids(db, report)
    db.commit()
    assert any("не прописано ни одного баркода" in p for p in request_upload(db, report, "admin"))


# ------------------------------------------------------------ справочник и себестоимость

def test_barcode_dict_answer_loads_the_catalog(db, epf_ready, exchange_dirs):
    task = onec.enqueue(db, "BARCODE_DICT")
    db.commit()
    onec.exchange_once(db)
    _answer(exchange_dirs, "fin_2", f"{task.key}|OK|справочник выгружен|BARCODE_DICT",
            barcodes="u1|A-1|Джинсы|111|46|синий\nu1|A-1|Джинсы|112|46|синий\nu2|B|Куртка «Зима|Лето»|222|50|чёрный\n")
    onec.exchange_once(db)
    db.refresh(task)
    assert task.status == "done", task.result_detail
    assert db.query(Barcode).count() == 3
    assert db.get(Product, "u2").name == "Куртка «Зима|Лето»"
    assert settings.get(db, settings.CATALOG_LOADED_AT)


def test_empty_barcode_dict_does_not_wipe_the_catalog(db, epf_ready, exchange_dirs):
    make_product(db, uid="u1", barcodes=("111",))
    task = onec.enqueue(db, "BARCODE_DICT")
    db.commit()
    onec.exchange_once(db)
    _answer(exchange_dirs, "fin_3", f"{task.key}|OK|справочник выгружен|BARCODE_DICT", barcodes="")
    onec.exchange_once(db)
    db.refresh(task)
    assert task.status == "failed"
    assert db.query(Barcode).count() == 1


def test_cost_answer_imports_usd_costs_and_keeps_manual_ones(db, epf_ready, exchange_dirs):
    make_product(db, uid="u1", barcodes=("111",))
    make_product(db, uid="u2", barcodes=("222",))
    db.add(ProductCost(uid_1c="u2", valid_from=date(2026, 1, 1), cost_minor=150000, currency="RUB",
                       source="excel"))
    db.commit()
    task = onec.enqueue(db, "EXPORT_COST_PRICES")
    db.commit()
    onec.exchange_once(db)
    _answer(exchange_dirs, "fin_4", f"{task.key}|OK|себестоимость выгружена|EXPORT_COST_PRICES",
            cost="u1|15.01\nu2|20.00\nu404|9.99\nмусор\n")
    onec.exchange_once(db)
    db.refresh(task)
    assert task.status == "done"
    assert "обновлено 1" in task.result_detail and "вручную (не тронуто) 1" in task.result_detail
    u1 = db.query(ProductCost).filter(ProductCost.uid_1c == "u1").one()
    assert (u1.cost_minor, u1.currency, u1.source, u1.valid_from) == (1501, "USD", "1c", today_local())
    u2 = db.query(ProductCost).filter(ProductCost.uid_1c == "u2").all()
    assert [(c.cost_minor, c.currency) for c in u2] == [(150000, "RUB")]


def test_same_cost_next_day_adds_no_history(db):
    from finapp.finance.costs import import_costs
    make_product(db, uid="u1", barcodes=())
    db.add(ProductCost(uid_1c="u1", valid_from=date(2026, 9, 1), cost_minor=1501, currency="USD",
                       source="1c"))
    db.commit()
    stats = import_costs(db, {"u1": 1501})
    assert stats["same"] == 1 and db.query(ProductCost).count() == 1


def test_broken_result_file_stays_for_the_next_pass(db, epf_ready, exchange_dirs, monkeypatch):
    task = onec.enqueue(db, "EXPORT_COST_PRICES")
    db.commit()
    onec.exchange_once(db)
    _answer(exchange_dirs, "fin_5", f"{task.key}|OK|ok|EXPORT_COST_PRICES", cost="u1|1.00\n")

    def boom(*_a, **_k):
        raise RuntimeError("диск")
    monkeypatch.setattr(onec.costs, "import_costs", boom)
    got = onec.exchange_once(db)
    assert got["failed_files"]
    assert (Path(exchange_dirs.ONEC_RESULTS_DIR) / "result_fin_5.txt").exists()


# ------------------------------------------------------------ контракт с обработкой 1С

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ONEC_TXT = REPO_ROOT / "1c" / "ОбменССайтом_МодульОбъекта.txt"

# Только в репозитории: на сервер каталог 1c/ не возят (модуль переносится в
# обработку Конфигуратором), и рядом с программой его нет.
repository_only = pytest.mark.skipif(not ONEC_TXT.exists(), reason="модуль 1С есть только в репозитории")


@repository_only
def test_onec_module_speaks_our_protocol():
    source = ONEC_TXT.read_text(encoding="utf-8")
    assert 'Команда = "SALES_REPORT"' in source
    assert 'Команда = "FIN_PING"' in source
    assert '"|OK|" + ВерсияМодуля() + "|FIN_PING"' in source
    assert 'Возврат "mark-4";' in source
    # Ответы на task_fin_* — в свой подкаталог results\finance.
    assert 'СтрНачинаетсяС(Метка, "fin_")' in source and 'Подкаталог = "finance"' in source
    assert '"|OK|" + ОчиститьПоле(Номера) + "|SALES_REPORT"' in source
    # По умолчанию документы не проводятся — бухгалтер проводит сам.
    assert 'Пар.Вставить("ПроводитьОтчетыПродаж", Ложь)' in source
