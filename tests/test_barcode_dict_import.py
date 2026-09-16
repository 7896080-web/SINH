"""Импорт справочника баркодов 1С (import_barcode_dict) в двух режимах.

- Частый прогон (full=False, раз в 15 мин): заводит только баркоды, уже
  присутствующие в каталогах площадок (быстро, не тащит ~154k каждый раз).
- Полный прогон (full=True, раз в неделю): заводит ВЕСЬ справочник 1С, чтобы
  сопоставление шло по полному справочнику даже при неполном каталоге.
Плюс самоопределение недельного режима по метке WorkerHeartbeat.
"""

from datetime import timedelta

from app.models import Product, Barcode, MappingConflict, PlatformCatalogItem, WorkerHeartbeat
from app.timeutils import now_utc
from app.workers.reconciliation import import_barcode_dict
from app.workers.scheduler import _should_run_full_barcode_import, FULL_BARCODE_IMPORT_EVERY


def _rows():
    return [
        {"uid_1c": "u-1", "barcode": "200000000001", "size": "XL", "color": "RED",
         "article": "ART-1", "name": "Свитшот"},
        {"uid_1c": "u-1", "barcode": "200000000002", "size": "XL", "color": "RED",
         "article": "ART-1", "name": "Свитшот"},  # второй баркод того же uid (пул)
        {"uid_1c": "u-2", "barcode": "200000000003", "size": "M", "color": "BLUE",
         "article": "ART-2", "name": "Футболка"},
    ]


def test_full_imports_all_even_without_catalog(db):
    # В каталогах площадок НЕТ ни одного из этих баркодов — full=True всё равно
    # заводит их: справочник 1С авторитетен и полон.
    stats = import_barcode_dict(db, _rows(), full=True)
    assert stats["full"] is True
    assert stats["products"] == 2
    assert stats["barcodes"] == 3
    assert db.query(Barcode).count() == 3
    assert {b.uid_1c for b in db.query(Barcode).filter(
        Barcode.barcode.in_(["200000000001", "200000000002"]))} == {"u-1"}


def test_filtered_imports_only_catalog_barcodes(db):
    # В каталоге площадки есть только один из трёх баркодов.
    db.add(PlatformCatalogItem(account_id=1, external_id="nm:1", barcode="200000000002"))
    db.commit()
    stats = import_barcode_dict(db, _rows())  # full=False по умолчанию
    assert stats["full"] is False
    assert stats["barcodes"] == 1
    assert db.query(Barcode).count() == 1
    assert db.query(Barcode).first().barcode == "200000000002"


def test_full_closes_matching_conflicts(db):
    db.add(MappingConflict(barcode="200000000001", account_id=1, attempts=3))
    db.commit()
    stats = import_barcode_dict(db, _rows(), full=True)
    assert stats["conflicts_cleared"] == 1
    assert db.query(MappingConflict).count() == 0


def test_idempotent(db):
    import_barcode_dict(db, _rows(), full=True)
    stats = import_barcode_dict(db, _rows(), full=True)
    assert stats["products"] == 0
    assert stats["barcodes"] == 0
    assert db.query(Product).count() == 2
    assert db.query(Barcode).count() == 3


def test_skips_blank_rows(db):
    rows = [
        {"uid_1c": "", "barcode": "200000000001"},
        {"uid_1c": "u-1", "barcode": ""},
        {"uid_1c": "u-1", "barcode": "200000000002", "size": "XL", "color": "RED",
         "article": "A", "name": "N"},
    ]
    stats = import_barcode_dict(db, rows, full=True)
    assert stats["barcodes"] == 1


def test_weekly_full_gate(db):
    # Метки ещё нет → первый прогон полный.
    assert _should_run_full_barcode_import(db) is True
    # Свежая метка → фильтрованный.
    hb = WorkerHeartbeat(worker_name="import_barcodes_full", last_run_at=now_utc(), last_success=True)
    db.add(hb)
    db.commit()
    assert _should_run_full_barcode_import(db) is False
    # Старше недели → снова полный.
    hb.last_run_at = now_utc() - FULL_BARCODE_IMPORT_EVERY - timedelta(hours=1)
    db.commit()
    assert _should_run_full_barcode_import(db) is True
