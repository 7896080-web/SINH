from datetime import datetime
from collections import defaultdict
from app.timeutils import now_utc

# Источник привязки, сделанной ДОГАДКОЙ по соседям в каталоге площадки. Отдельным
# именем, потому что приём справочника 1С отличает её от остальных и только её
# имеет право перебить (`reconciliation.import_barcode_dict`).
POOL_GUESS_SOURCE = "pool_match"

from sqlalchemy.orm import Session

from app.models import PlatformCatalogItem, Barcode, MappingConflict, PlatformAccount
from app.workers.platform_clients.base import PlatformClient


def load_platform_catalog(db: Session, client: PlatformClient, account: PlatformAccount) -> dict:
    """Тянет полную спецификацию карточек кабинета (баркод, артикул,
    название), сохраняет снимок в PlatformCatalogItem и заодно регистрирует
    в «КонфликтыСопоставления» те баркоды кабинета, которых ещё нет в нашей
    таблице «Баркоды» — чтобы конфликт стало видно ДО того, как по нему
    придёт первый заказ, а не после."""

    items = client.get_catalog_items()
    stats = {"fetched": len(items), "already_mapped": 0, "pool_matched": 0,
             "new_conflicts": 0, "known_conflicts": 0, "no_barcode": 0}

    # Ключ строки каталога — БАРКОД (у WB один external_id/nmID охватывает
    # несколько баркодов). Держим карту barcode -> row для этого кабинета,
    # чтобы апсертить по баркоду и не плодить дубли при повторной загрузке
    # и при нескольких баркодах одной карточки в пределах одного прохода.
    existing = {
        r.barcode: r
        for r in db.query(PlatformCatalogItem).filter(PlatformCatalogItem.account_id == account.id).all()
        if r.barcode
    }

    # Пул баркодов размер-цвета: external_id (у WB nmID:chrtID) — это РОВНО один
    # размер-цвет SKU. Собираем баркоды каждого пула ИЗ ЭТОЙ выгрузки и сопоставляем
    # пул с uid 1С ТОЛЬКО если 1С-соседи по пулу дают ОДИН uid (пул не объединяется
    # с другими SKU). Тогда остальные баркоды пула — альтернативные баркоды того же
    # размер-цвета, а не конфликты. Неоднозначный пул (соседи → разные uid) не мержим.
    pool_barcodes = defaultdict(set)
    for it in items:
        if it.external_id and it.barcode:
            pool_barcodes[it.external_id].add(it.barcode)
    pool_uid = {}
    for extid, bcs in pool_barcodes.items():
        uids = {b.uid_1c for b in db.query(Barcode).filter(Barcode.barcode.in_(bcs)).all()}
        if len(uids) == 1:
            pool_uid[extid] = next(iter(uids))

    # Баркоды, заведённые догадкой В ЭТОМ ЖЕ проходе: см. ниже, почему запроса
    # к базе для этого мало.
    guessed_here: set[str] = set()
    # То же для конфликтов сопоставления: см. ниже.
    conflicted_here: set[str] = set()
    for item in items:
        if not item.barcode:
            stats["no_barcode"] += 1
            continue

        row = existing.get(item.barcode)
        if row is None:
            row = PlatformCatalogItem(account_id=account.id, barcode=item.barcode)
            db.add(row)
            existing[item.barcode] = row

        row.external_id = item.external_id
        row.article = item.article
        row.name = item.name
        row.fetched_at = now_utc()

        existing_barcode = db.query(Barcode).filter(Barcode.barcode == item.barcode).first()
        if existing_barcode is not None:
            stats["already_mapped"] += 1
            continue

        # Баркод не совпал напрямую, но его пул размер-цвета однозначно ведёт к uid
        # (сосед по external_id уже в 1С) → регистрируем как АЛЬТЕРНАТИВНЫЙ баркод
        # того же размер-цвета (сопоставление по штрих-коду размер-цвета), а не
        # заводим конфликт. Существующий конфликт по этому баркоду закрываем.
        uid = pool_uid.get(item.external_id)
        if uid is not None:
            if item.barcode in guessed_here:
                # Тот же баркод второй раз в ОДНОЙ выгрузке. Запрос выше его не
                # найдёт: сессия живёт с `autoflush=False`, и первый `db.add` до
                # базы ещё не дошёл. Второй `INSERT` на коммите давал
                # `UNIQUE constraint failed: barcodes.barcode` — и падала ВСЯ
                # загрузка каталога, причём падала бы каждый следующий раз, пока
                # площадка отдаёт ту же выгрузку. Каталог при этом молча
                # устаревает: по нему считаются chrtId для WB и variant_id для
                # Kit, то есть ключи, которыми уходит остаток.
                stats["already_mapped"] += 1
                continue
            guessed_here.add(item.barcode)
            db.add(Barcode(barcode=item.barcode, uid_1c=uid, source_platform=POOL_GUESS_SOURCE))
            db.query(MappingConflict).filter(
                MappingConflict.barcode == item.barcode,
            ).delete(synchronize_session=False)
            stats["pool_matched"] += 1
            continue

        conflict = db.query(MappingConflict).filter(
            MappingConflict.barcode == item.barcode, MappingConflict.account_id == account.id,
        ).first()
        if item.barcode in conflicted_here:
            # Повтор в ЭТОЙ ЖЕ выгрузке: конфликт по нему мы уже завели строкой
            # выше, но запрос его не видит — `autoflush=False`, до базы он ещё не
            # дошёл. Уникального ограничения здесь нет, поэтому получилось бы не
            # падение, а ДУБЛИ строк разбора: оператор разбирал бы один и тот же
            # баркод дважды, а счётчик новых конфликтов врал бы в ту же сторону.
            stats["known_conflicts"] += 1
        elif conflict is None:
            conflicted_here.add(item.barcode)
            db.add(MappingConflict(barcode=item.barcode, account_id=account.id, attempts=1))
            stats["new_conflicts"] += 1
        else:
            conflict.last_seen = now_utc()
            stats["known_conflicts"] += 1

    db.commit()
    return stats
