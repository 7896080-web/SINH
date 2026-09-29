import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

from conftest import fixture_bytes
from markapp import onec, settings
from markapp import supplies as S
from markapp.catalog import import_catalog
from markapp.models import OnecTask, Supply
from markapp.timeutils import now_utc


@pytest.fixture
def catalog(db):
    import_catalog(db, fixture_bytes("lamoda_catalog_full_2026-09-28.xlsx"))
    db.commit()


def _supply(db, number=None, name="lamoda_shipment_input_typical.xlsx") -> Supply:
    parsed = S.parse_input(fixture_bytes(name))
    number = number or S.next_number(db)
    s = S.create_supply(db, parsed, organization_id=settings.lamoda_org(db).id, number=number,
                        doc_number=number, supply_date=date(2026, 10, 2), filename=name, username="t")
    db.commit()
    return s


def _ready(db):
    """Обработка 1С ответила на PING."""
    settings.put(db, onec.EPF_VERSION, "mark-1")
    db.commit()


# --- поставка -------------------------------------------------------------------

def test_supply_takes_ean_and_price_from_catalog(db, catalog):
    s = _supply(db)
    assert s.number == "12560"
    assert all(r.ean and r.price for r in s.rows)
    t = S.totals(s)
    # «Итого стоимость» Lamoda по этой поставке (12550 — тот же состав).
    assert (t["articles"], t["units"], t["money"]) == (80, 338, Decimal("4337500.00"))
    assert not S.blocking_problems(s)


def test_numbering_never_repeats_even_after_delete(db, catalog):
    s = _supply(db)
    assert s.number == "12560"
    db.delete(s)
    db.commit()
    assert S.next_number(db) == "12570"


def test_numbers_follow_lamoda_rules_and_are_unique(db, catalog):
    _supply(db, "12560")
    errs = S.validate_numbers(db, "12560", "12560")
    assert any("уже есть" in e for e in errs)
    assert S.validate_numbers(db, "12 560", "доку")      # пробел и кириллица запрещены


def test_supply_without_catalog_is_blocked(db):
    s = _supply(db)
    assert all(not r.ean for r in s.rows)
    assert any("без штрихкода" in p for p in S.blocking_problems(s))


def test_after_movement_supply_is_frozen(db, catalog):
    s = _supply(db)
    s.status = "moved"
    db.commit()
    with pytest.raises(S.SupplyError):
        S.update_row_qty(s, s.rows[0].id, 1)
    with pytest.raises(S.SupplyError):
        S.refresh_from_catalog(db, s)


def test_edit_returns_supply_to_draft(db, catalog):
    s = _supply(db)
    s.status = "checked"
    S.update_row_qty(s, s.rows[0].id, 2)
    assert s.status == "draft"


# --- строка задания 1С ----------------------------------------------------------

def test_movement_line_has_no_date_and_aggregates_barcodes(db, catalog):
    s = _supply(db)
    line = onec.supply_line("SUPPLY_MOVEMENT", onec.movement_order_id(s), s)
    parts = line.split("|")
    assert parts[:4] == ["SUPPLY_MOVEMENT", "lamoda-12560", "ЦС Склад", "Lamoda_Склад"]
    assert len(parts) == 6
    positions = dict(p.split(":") for p in parts[4].split(";"))
    assert sum(int(q) for q in positions.values()) == 338
    assert len(positions) == 80
    # Поля даты нет: дата поставки едет только в комментарий.
    assert parts[5] == "поставка 12560 от 02.10.2026"


def test_only_own_commands_go_to_1c(db):
    for foreign in ("CREATE_MOVEMENT", "CANCEL_MOVEMENT", "CONFIRM_MOVEMENT", "EXPORT_STOCK_ON_HAND"):
        with pytest.raises(onec.OnecError):
            onec.enqueue(db, foreign, "x", f"{foreign}|x")


def test_separator_in_value_is_refused(db, catalog):
    s = _supply(db)
    s.rows[0].ean = "2000|1"
    with pytest.raises(onec.OnecError):
        onec.supply_line("SUPPLY_CHECK", "x", s)


# --- публикация -----------------------------------------------------------------

def test_supply_tasks_wait_for_ping_but_ping_goes(db, catalog, exchange_dirs):
    s = _supply(db)
    onec.enqueue_check(db, s)
    onec.enqueue_ping(db)
    db.commit()
    assert onec.publish_pending(db) == 1
    files = list(exchange_dirs.ONEC_TASKS_DIR.glob("*"))
    assert len(files) == 1
    assert files[0].name.startswith("task_mark_") and files[0].suffix == ".txt"
    assert files[0].read_text(encoding="utf-8").startswith("PING|ping-")
    _ready(db)
    assert onec.publish_pending(db) == 1
    assert len(list(exchange_dirs.ONEC_TASKS_DIR.glob("task_mark_*.txt"))) == 2
    assert not list(exchange_dirs.ONEC_TASKS_DIR.glob("*.part"))


def test_test_supply_never_reaches_1c(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    s.is_test = True
    onec.enqueue_check(db, s)
    db.commit()
    assert onec.publish_pending(db) == 0
    assert not list(exchange_dirs.ONEC_TASKS_DIR.glob("*"))


# --- ответы ---------------------------------------------------------------------

def _answer(dirs, label, result, check=""):
    (dirs.ONEC_RESULTS_DIR / f"result_{label}.txt").write_text(result, encoding="utf-8-sig")
    if check:
        (dirs.ONEC_RESULTS_DIR / f"supplycheck_{label}.txt").write_text(check, encoding="utf-8-sig")


def _check_file(s, short_ean=None):
    lines = []
    seen = set()
    for r in s.rows:
        if r.ean in seen:
            continue
        seen.add(r.ean)
        st = "short" if r.ean == short_ean else "ok"
        lines.append(f"{r.ean}|id-{r.position}|{r.supplier_sku[:4]}|{r.supplier_sku}|L|Чёрный|10|{r.qty}|{st}")
    return "\n".join(lines)


def test_check_ok_marks_supply_checked_and_fills_rows(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    task = onec.enqueue_check(db, s)
    db.commit()
    onec.publish_pending(db)
    _answer(exchange_dirs, "mark_1", f"{task.order_id}|OK||SUPPLY_CHECK", _check_file(s))
    stats = onec.collect_results(db)
    assert stats["ok"] == 1
    db.refresh(s)
    assert s.status == "checked"
    assert all(r.onec_status == "ok" and r.onec_stock == 10 for r in s.rows)
    # Разобранное — в архив, и только после коммита.
    assert not list(exchange_dirs.ONEC_RESULTS_DIR.glob("*"))
    assert len(list(exchange_dirs.ONEC_ARCHIVE_DIR.glob("*"))) == 2


def test_check_error_keeps_draft_and_shows_shortage(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    task = onec.enqueue_check(db, s)
    db.commit()
    onec.publish_pending(db)
    ean = s.rows[0].ean
    _answer(exchange_dirs, "mark_1", f"{task.order_id}|ERROR|не хватает 1 позиции|SUPPLY_CHECK",
            _check_file(s, short_ean=ean))
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "draft"
    assert s.rows[0].onec_status == "short"


def test_stale_check_answer_does_not_approve_changed_supply(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    old = onec.enqueue_check(db, s)
    new = onec.enqueue_check(db, s)
    db.commit()
    onec.publish_pending(db)
    _answer(exchange_dirs, "mark_1", f"{old.order_id}|OK||SUPPLY_CHECK", _check_file(s))
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "draft"          # ответ на прежнюю проверку статус не двигает
    _answer(exchange_dirs, "mark_2", f"{new.order_id}|OK||SUPPLY_CHECK", _check_file(s))
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "checked"


def test_movement_ok_freezes_supply(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    s.status = "checked"
    task = onec.enqueue_movement(db, s)
    db.commit()
    onec.publish_pending(db)
    _answer(exchange_dirs, "mark_1", "lamoda-12560|OK|ЦБ000001234|SUPPLY_MOVEMENT")
    onec.collect_results(db)
    db.refresh(s)
    db.refresh(task)
    assert (s.status, s.onec_document, task.status) == ("moved", "ЦБ000001234", "done")


def test_movement_refused_returns_to_draft_for_new_check(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    s.status = "checked"
    onec.enqueue_movement(db, s)
    db.commit()
    onec.publish_pending(db)
    _answer(exchange_dirs, "mark_1", "lamoda-12560|ERROR|не хватает 2000932309880: нужно 5, есть 4|SUPPLY_MOVEMENT")
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "draft"


def test_timeout_then_late_answer_is_accepted(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    s.status = "checked"
    task = onec.enqueue_movement(db, s)
    db.commit()
    onec.publish_pending(db)
    task.sent_at = now_utc() - timedelta(hours=1)
    db.commit()
    assert onec.mark_timeouts(db) == 1
    _answer(exchange_dirs, "mark_1", "lamoda-12560|OK|ЦБ1|SUPPLY_MOVEMENT")
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "moved"


def test_unmatched_answer_is_counted_and_archived(db, exchange_dirs):
    _answer(exchange_dirs, "mark_x", "lamoda-99999|OK|ЦБ9|SUPPLY_MOVEMENT")
    stats = onec.collect_results(db)
    assert stats["unmatched"] == 1
    assert not list(exchange_dirs.ONEC_RESULTS_DIR.glob("*"))


def test_answer_file_survives_failed_commit(db, catalog, exchange_dirs, monkeypatch):
    """Сбой записи в базу не уносит ответ: файл остаётся и разберётся потом."""
    _ready(db)
    s = _supply(db)
    task = onec.enqueue_check(db, s)
    db.commit()
    onec.publish_pending(db)
    _answer(exchange_dirs, "mark_1", f"{task.order_id}|OK||SUPPLY_CHECK", _check_file(s))

    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError):
        onec.collect_results(db)
    assert (exchange_dirs.ONEC_RESULTS_DIR / "result_mark_1.txt").exists()


def test_ping_answer_enables_supply_tasks(db, exchange_dirs):
    task = onec.enqueue_ping(db)
    db.commit()
    onec.publish_pending(db)
    assert not onec.epf_ready(db)
    _answer(exchange_dirs, "mark_1", f"{task.order_id}|OK|mark-1|PING")
    onec.collect_results(db)
    assert onec.epf_ready(db)
    assert settings.get(db, onec.EPF_VERSION) == "mark-1"


def test_results_of_sync_admin_are_not_touched(db, exchange_dirs):
    """Файлы sync_admin лежат уровнем выше — программа их не видит и не трогает."""
    parent = exchange_dirs.ONEC_RESULTS_DIR.parent
    theirs = parent / "result_20260929120000000000.txt"
    theirs.write_text("abc|OK|ЦБ1|CREATE_MOVEMENT", encoding="utf-8")
    onec.collect_results(db)
    assert theirs.exists()
    os.remove(theirs)


# --- сопоставление по штрихкоду (правило sync_admin) ----------------------------

def test_one_barcode_on_two_articles_blocks_the_supply(db, catalog):
    """Штрихкод — ключ одного товара 1С. Два артикула Lamoda с одним штрихкодом
    1С не различит: оба уедут одним товаром, а перемещение не отменяется."""
    from markapp.models import CatalogItem
    s = _supply(db)
    # Типовая поставка чиста: каждый штрихкод у одного артикула.
    assert not S.shared_barcodes(s) and not any("штрихкод" in p for p in S.blocking_problems(s))
    a, b = s.rows[0], s.rows[1]
    db.query(CatalogItem).filter(CatalogItem.supplier_sku == b.supplier_sku).one().ean = a.ean
    db.commit()
    S.refresh_from_catalog(db, s)
    db.commit()
    problems = S.blocking_problems(s)
    assert any("один штрихкод у разных артикулов" in p and a.ean in p for p in problems)
    assert f"«{b.supplier_sku}»" in a.warnings and f"«{a.supplier_sku}»" in b.warnings


def test_two_barcodes_of_one_1c_item_are_noted_not_blocked(db, catalog, exchange_dirs):
    """Разные штрихкоды одного товара 1С — альтернативные баркоды: остаток
    общий, 1С сложит нужное. Замечание, а не отказ."""
    _ready(db)
    s = _supply(db)
    task = onec.enqueue_check(db, s)
    db.commit()
    onec.publish_pending(db)
    a, b = s.rows[0], s.rows[1]
    check = _check_file(s).replace(f"|id-{b.position}|", f"|id-{a.position}|")
    _answer(exchange_dirs, "mark_1", f"{task.order_id}|OK||SUPPLY_CHECK", check)
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "checked"
    notes = S.shared_onec_items(s)
    assert set(notes) == {a.id, b.id}
    assert b.supplier_sku in notes[a.id] and "суммой" in notes[a.id]


def test_ambiguous_barcode_answer_is_shown(db, catalog, exchange_dirs):
    _ready(db)
    s = _supply(db)
    task = onec.enqueue_check(db, s)
    db.commit()
    onec.publish_pending(db)
    ean = s.rows[0].ean
    check = _check_file(s).replace(
        next(l for l in _check_file(s).splitlines() if l.startswith(ean)),
        f"{ean}||||||0|{s.rows[0].qty}|ambiguous")
    _answer(exchange_dirs, "mark_1",
            f"{task.order_id}|ERROR|штрихкод {ean} записан на нескольких товарах 1С|SUPPLY_CHECK", check)
    onec.collect_results(db)
    db.refresh(s)
    assert s.status == "draft" and s.rows[0].onec_status == "ambiguous"
