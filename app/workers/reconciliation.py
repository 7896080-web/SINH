from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Product, Barcode, FtpTask, FtpTaskStatus, ReconciliationLog,
    ReconciliationClassification, DispatchQueueItem, SyncSetting,
    PlatformCatalogItem, MappingConflict,
)

LARGE_DELTA_ABSOLUTE = 5
LARGE_DELTA_RATIO = 0.3


def classify_delta(delta: int, python_stock: int) -> ReconciliationClassification:
    """Чистая функция — раздел 9, таблица классификации дельты.
    Вынесена отдельно от run_reconciliation специально, чтобы её можно было
    протестировать без БД и без сети (см. tests/test_reconciliation.py)."""

    if delta == 0:
        return ReconciliationClassification.normal

    abs_delta = abs(delta)
    ratio = abs_delta / python_stock if python_stock > 0 else 1.0

    if abs_delta > LARGE_DELTA_ABSOLUTE or ratio > LARGE_DELTA_RATIO:
        return ReconciliationClassification.needs_review

    return ReconciliationClassification.auto_plus if delta > 0 else ReconciliationClassification.auto_minus


def _in_flight_adjustment(db: Session, uid_1c: str) -> int:
    """Сколько 'в пути' на FTP-канале для этого товара прямо сейчас:
    незавершённые CREATE_MOVEMENT увеличивают ожидаемый остаток в 1С
    (мы уже списали у себя, 1С ещё нет), незавершённые CANCEL_MOVEMENT —
    уменьшают (мы уже вернули, 1С ещё нет)."""

    barcodes = {b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c == uid_1c).all()}
    if not barcodes:
        return 0

    open_tasks = db.query(FtpTask).filter(
        FtpTask.barcode.in_(barcodes),
        FtpTask.status.in_([FtpTaskStatus.pending, FtpTaskStatus.sent]),
        FtpTask.is_test.is_(False),  # тестовые задания не должны искажать сверку
    ).all()

    adjustment = 0
    for t in open_tasks:
        if t.command == "CREATE_MOVEMENT":
            adjustment += t.quantity or 0
        elif t.command == "CANCEL_MOVEMENT":
            adjustment -= t.quantity or 0
    return adjustment


def import_product_master(db: Session, rows: list[dict]) -> dict:
    """Заводит/обновляет ассортимент из полной выгрузки 1С (см.
    ftp_channel.parse_stock_export_rows). Создаёт недостающие Product (остаток
    = кол-во из 1С, трансляция OFF по умолчанию) и Barcode; у существующих
    обновляет артикул/наименование/размер/цвет, НЕ трогая остаток (им владеет
    сверка) и ручные поля (резерв/override/трансляция). Идемпотентно.

    Нужно, потому что штатно Product в приложении не создаётся нигде: 1С —
    хозяин ассортимента, но кода заведения товаров в проекте не было."""
    stats = {"created": 0, "updated": 0, "barcodes": 0}
    seen = {b.barcode for b in db.query(Barcode).all()}
    for r in rows:
        uid = (r.get("uid_1c") or "").strip()
        if not uid:
            continue
        product = db.query(Product).filter(Product.uid_1c == uid).first()
        if product is None:
            product = Product(
                uid_1c=uid, article=r.get("article"), name=r.get("name"),
                stock_on_hand=r.get("quantity") or 0,
                size=(r.get("size") or None), color=(r.get("color") or None),
            )
            db.add(product)
            stats["created"] += 1
        else:
            if r.get("article"):
                product.article = r["article"]
            if r.get("name"):
                product.name = r["name"]
            if r.get("size"):
                product.size = r["size"]
            if r.get("color"):
                product.color = r["color"]
            stats["updated"] += 1
        for code in r.get("barcodes", []):
            if code and code not in seen:
                db.add(Barcode(barcode=code, uid_1c=uid, source_platform="1c_export"))
                seen.add(code)
                stats["barcodes"] += 1
    db.commit()
    return stats


def import_barcode_dict(db: Session, rows: list[dict], full: bool = False) -> dict:
    """Приём справочника баркодов из 1С (barcodes_*.txt). Два режима:

    - full=False (по умолчанию, частый прогон раз в 15 мин): заводит ТОЛЬКО
      баркоды, которые уже есть в каталогах площадок (PlatformCatalogItem) —
      быстро, не тащит весь справочник (~154k) в базу на каждом прогоне.
    - full=True (полный прогон раз в неделю): заводит ВЕСЬ справочник 1С — база
      баркодов становится полным, авторитетным отражением 1С и НЕ зависит от
      того, что успели догрузить каталоги площадок. Это добирает баркоды,
      которых на площадках ещё нет, чтобы сопоставление по полному справочнику
      1С работало даже при неполном каталоге (любой пул площадки, где хотя бы
      один баркод совпал с 1С, тогда привяжется).

    Создаёт недостающие Product (остаток 0, трансляция OFF) и Barcode с
    размером/цветом и закрывает соответствующие MappingConflict.
    Дедупликация по uid. Идемпотентно."""
    catalog_bcs = None if full else {
        r[0] for r in db.query(PlatformCatalogItem.barcode).distinct().all() if r[0]
    }
    existing_uids = {r[0] for r in db.query(Product.uid_1c).all()}
    existing_bcs = {b.barcode for b in db.query(Barcode).all()}

    stats = {"products": 0, "barcodes": 0, "conflicts_cleared": 0, "full": full}
    resolved = set()
    for r in rows:
        uid = (r.get("uid_1c") or "").strip()
        barcode = (r.get("barcode") or "").strip()
        if not uid or not barcode:
            continue
        if catalog_bcs is not None and barcode not in catalog_bcs:
            continue
        size = (r.get("size") or "").strip()
        color = (r.get("color") or "").strip()

        if uid not in existing_uids:
            db.add(Product(uid_1c=uid, article=r.get("article"), name=r.get("name"),
                           stock_on_hand=0, size=(size or None), color=(color or None)))
            existing_uids.add(uid)
            stats["products"] += 1
        if barcode not in existing_bcs:
            db.add(Barcode(barcode=barcode, uid_1c=uid, source_platform="1c_dict"))
            existing_bcs.add(barcode)
            stats["barcodes"] += 1
        resolved.add(barcode)

    db.flush()
    resolved = list(resolved)
    for i in range(0, len(resolved), 500):
        stats["conflicts_cleared"] += db.query(MappingConflict).filter(
            MappingConflict.barcode.in_(resolved[i:i + 500])
        ).delete(synchronize_session=False)
    db.commit()
    return stats


# Доля прошлых ненулевых товаров, ниже которой снимок считается подозрительным и
# обнуление по отсутствию НЕ применяется: лучше не тронуть остаток, чем обнулить
# весь ассортимент из-за обрезанного или подсунутого вручную файла.
MIN_SNAPSHOT_COVERAGE = 0.5


def run_reconciliation(db: Session, stock_from_1c: dict[str, int],
                       missing_means_zero: bool = False) -> dict:
    """stock_from_1c: {баркод: количество на ЦС} — результат периодической
    выгрузки из старой базы (раздел 8). Раз в час, согласно спецификации.

    `missing_means_zero` — выгрузка является ПОЛНЫМ снимком склада ЦС. Тогда товар,
    которого в снимке нет, физически распродан в ноль: виртуальная таблица
    «ТоварыНаСкладах.Остатки» нулевые позиции не возвращает, и без этого флага такой
    товар не сверялся бы никогда — приложение вечно транслировало бы на площадки
    последнее ненулевое число (прямой оверселл)."""

    stats = {"normal": 0, "auto_plus": 0, "auto_minus": 0, "needs_review": 0,
             "unmatched_barcodes": 0, "zeroed_missing": 0}

    # Группируем полученные из 1С количества по uid_1c (может быть несколько
    # баркодов на один товар — считаем максимум, т.к. это один физический остаток)
    uid_to_actual = {}
    for barcode, qty in stock_from_1c.items():
        row = db.query(Barcode).filter(Barcode.barcode == barcode).first()
        if row is None:
            stats["unmatched_barcodes"] += 1
            continue
        uid_to_actual[row.uid_1c] = max(uid_to_actual.get(row.uid_1c, 0), qty)

    if missing_means_zero and uid_to_actual:
        # Товары с баркодами и ненулевым остатком, которых в снимке нет.
        known = {uid for (uid,) in db.query(Product.uid_1c)
                 .filter(Product.stock_on_hand != 0)
                 .join(Barcode, Barcode.uid_1c == Product.uid_1c).distinct()}
        missing = known - set(uid_to_actual)
        if known and len(uid_to_actual) < len(known) * MIN_SNAPSHOT_COVERAGE:
            # Снимок покрывает меньше половины прежних ненулевых товаров — похоже на
            # обрезанную или подложенную вручную выгрузку. Ничего не обнуляем.
            stats["snapshot_suspicious"] = len(uid_to_actual)
        else:
            for uid in missing:
                uid_to_actual[uid] = 0
            stats["zeroed_missing"] = len(missing)

    for uid_1c, actual_1c in uid_to_actual.items():
        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            continue

        in_flight = _in_flight_adjustment(db, uid_1c)
        expected_1c = product.stock_on_hand + in_flight
        delta = actual_1c - expected_1c

        classification = classify_delta(delta, product.stock_on_hand)

        log = ReconciliationLog(
            uid_1c=uid_1c, python_stock=product.stock_on_hand, in_flight=in_flight,
            expected_1c=expected_1c, actual_1c=actual_1c, delta=delta,
            classification=classification,
        )
        db.add(log)
        stats[classification.value] += 1

        if delta != 0:
            # По решению оператора применяем ЛЮБОЕ движение по складу (приход/расход)
            # автоматически, независимо от величины. classify_delta оставлен для
            # журнала (крупные по-прежнему видно как needs_review), но остаток и
            # ручную цифру двигаем всегда.
            product.stock_on_hand = actual_1c

            # Ручная цифра трансляции (transmit_override) «дышит» вместе со складом:
            # приход прибавляет, расход убавляет — ровно на delta. Заказы в delta НЕ
            # входят (учтены через in_flight и уже двигают override в момент заказа),
            # поэтому задвоения нет. max(0, …) не даёт уйти в минус.
            if product.transmit_override is not None:
                product.transmit_override = max(0, product.transmit_override + delta)

            log.resolved = True

            # Рассылаем актуальное значение на включённые кабинеты (дефицит — риск
            # оверселла, поэтому сразу, не ждём батч-цикл). Если задан override,
            # dispatch отправит именно его (см. _quantity_to_send).
            enabled_platforms = db.query(SyncSetting).filter(
                SyncSetting.uid_1c == uid_1c, SyncSetting.enabled.is_(True),
            ).all()
            for setting in enabled_platforms:
                db.add(DispatchQueueItem(
                    uid_1c=uid_1c, account_id=setting.account_id, quantity=actual_1c,
                    reason="reconciliation",
                ))

    db.commit()
    return stats
