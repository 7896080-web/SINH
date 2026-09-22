from datetime import datetime
from collections import defaultdict
from app.timeutils import now_utc

# Источник привязки, сделанной ДОГАДКОЙ по соседям в каталоге площадки. Отдельным
# именем, потому что приём справочника 1С отличает её от остальных и только её
# имеет право перебить (`reconciliation.import_barcode_dict`).
POOL_GUESS_SOURCE = "pool_match"

import logging

from sqlalchemy.orm import Session

from app.broadcast_gate import drop_recalc_mark
from app.models import (Barcode, MappingConflict, PlatformAccount, PlatformCatalogItem,
                        Product)
from app.workers.platform_clients.base import PlatformClient

logger = logging.getLogger("sync_worker")


def load_platform_catalog(db: Session, client: PlatformClient, account: PlatformAccount) -> dict:
    """Тянет полную спецификацию карточек кабинета (баркод, артикул,
    название), сохраняет снимок в PlatformCatalogItem и заодно регистрирует
    в «КонфликтыСопоставления» те баркоды кабинета, которых ещё нет в нашей
    таблице «Баркоды» — чтобы конфликт стало видно ДО того, как по нему
    придёт первый заказ, а не после."""

    items = client.get_catalog_items()
    # Выдачу оборвал наш защитный предел страниц, а не конец данных. Карточки,
    # не попавшие в обрезанную выдачу, остаются со СТАРЫМИ данными или не
    # заводятся вовсе — а снимок при этом выглядит свежим: `fetched_at` у
    # попавших обновлён. Дальше по этому снимку считаются ключи отправки: у WB
    # chrtId (нет его — остаток уходит баркодом, а WB умеет приём баркода
    # отключить), у Kit variant_id (нет — позиция не уедет вовсе и закроется
    # терминально). Молчать об этом нельзя: клиенты признак поднимают
    # (`last_truncated`), а здесь его до сих пор никто не спрашивал.
    truncated = bool(getattr(client, "last_truncated", False))
    stats = {"fetched": len(items), "already_mapped": 0, "pool_matched": 0,
             "recalc_dropped": 0,
             "new_conflicts": 0, "known_conflicts": 0, "no_barcode": 0,
             "revived": 0,
             "truncated": truncated}

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
            # Набор баркодов товара только что изменился — «актуализирован»
            # снимаем, как это делают ручная переподвязка и справочник 1С.
            #
            # Без этого догадка тихо открывала оверселл: расчёт собирал заказы по
            # ПРЕЖНЕМУ набору, продажи по новому баркоду он не видел, а догнать
            # их нечем — товар числится актуализированным, `catch_up_product` по
            # нему не зовут, живой опрос старый заказ уже не принесёт. Остаток
            # завышен ровно на эти продажи, ворота открыты, ступень 2 молчит.
            # Заодно этой же строкой удаляется ЕДИНСТВЕННЫЙ след — конфликт
            # сопоставления со счётчиком попыток.
            if drop_recalc_mark(db.query(Product).filter(Product.uid_1c == uid).first()):
                stats["recalc_dropped"] += 1
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
            # attempts=0, а НЕ 1: счётчик означает «сколько заказов по этому
            # баркоду мы не смогли разнести», и здесь их ноль — строку завела
            # выгрузка каталога, чтобы конфликт стало видно ДО первого заказа.
            # Единица тут была прямой неправдой: отчёт складывает `attempts` и
            # печатает «заказов по ним N», а следствие обещает «остаток завышен
            # ровно на эти продажи» — продаж не было ни одной, товара в 1С нет,
            # завышать нечего. `resolve_barcode` увеличит счётчик, когда заказ
            # действительно придёт, и строка сама переедет в нужную находку.
            db.add(MappingConflict(barcode=item.barcode, account_id=account.id, attempts=0))
            stats["new_conflicts"] += 1
        else:
            conflict.last_seen = now_utc()
            stats["known_conflicts"] += 1

    db.commit()

    stats["revived"] = revive_after_catalog(db, client, account)
    return stats


def revive_after_catalog(db: Session, client: PlatformClient,
                         account: PlatformAccount) -> int:
    """Поднять пары, закрытые из-за ОТСУТСТВИЯ этого самого каталога.

    Дефект, найденный 22.09 на живом кабинете КИТ. Последовательность вся
    штатная: завели карточку на площадке, включили трансляцию, расчёт прошёл и
    поставил пару в очередь — а каталог кабинета выгружается раз в сутки и про
    новую карточку ещё не знает. `variant_id` нет, рассылка закрывает позицию
    ТЕРМИНАЛЬНО (иначе Kit забракует весь запрос целиком), и через четыре минуты
    приходит каталог, в котором ключ уже есть. Дальше — тишина: запись
    терминальна, повтора не будет, следующая отправка случится, только когда
    изменится остаток. У зимней куртки это месяцы. Остаток не уедет НИКОГДА,
    хотя все условия давно выполнены, и заметить это можно только придя смотреть
    глазами.

    Тот же случай, что `recalc_covered` у расчёта: ворота открыло именно это
    событие, а запись, справедливо получившая отказ раньше, осталась лежать —
    значит поднимать её этому событию.

    ЧТО ИМЕННО ПОДНИМАЕМ, и почему не всё подряд. Два разных отказа помечены
    `card_missing`, и лечатся они разным:

      * НАШ отказ — рассылка не нашла ключа, которым адресует эта площадка, и
        в запрос позиция не пошла вовсе. `sent_sku` при этом пуст: он пишется
        только тем позициям, которые реально уехали (`dispatch`). Вот это и
        лечится каталогом.
      * Отказ ПЛОЩАДКИ — запрос ушёл, а она ответила «такого sku на складе
        нет» (у WB это `409 NotFound`). `sent_sku` заполнен. Каталог тут ни при
        чём: ключ был и есть, а карточки на складе площадки нет. Подними мы
        такую пару — сожгли бы запрос и закрыли её снова, а находка отчёта
        «Площадка не знает наш sku» замигала бы на ровном месте.

    Различаем ДАННЫМИ (`sent_sku IS NULL`), а не текстом ошибки: тексты у трёх
    площадок разные, и на этом уже обожглись — см. историю `card_missing`.

    И поднимаем только те пары, у которых ключ ТЕПЕРЬ действительно есть. Иначе
    каждая суточная выгрузка гоняла бы по кругу карточки, которых на площадке
    нет вовсе, — а их, по живому кабинету, под сотню.

    Ставим через `enqueue_full_resend`, то есть со всеми гейтами: товар без
    трансляции и кабинет вне расчёта не поедут. Обойти их здесь значило бы
    отправить ноль на живую карточку — ровно то, от чего гейты и стоят.
    """
    from app.transmit import enqueue_full_resend
    from app.workers.dispatch import push_identifier
    from app.models import DispatchQueueItem, DispatchStatus

    stock_key = getattr(client, "stock_key", "barcode")
    uids = {row[0] for row in db.query(DispatchQueueItem.uid_1c).filter(
        DispatchQueueItem.account_id == account.id,
        DispatchQueueItem.status == DispatchStatus.error,
        DispatchQueueItem.card_missing.is_(True),
        DispatchQueueItem.sent_sku.is_(None),
        DispatchQueueItem.is_test.is_(False),
    ).distinct().all()}
    if not uids:
        return 0

    revived = 0
    for uid in sorted(uids):
        if not push_identifier(db, uid, account.id, stock_key):
            continue                      # ключа как не было, так и нет
        if enqueue_full_resend(db, uid, account.id, reason="catalog_loaded"):
            revived += 1
    if revived:
        db.commit()
        logger.info("каталог «%s»: поднято пар, ждавших ключа отправки: %d",
                    account.name, revived)
    return revived
