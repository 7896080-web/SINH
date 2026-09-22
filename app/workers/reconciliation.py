from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.audit import log_action
from app.workers.catalog_sync import POOL_GUESS_SOURCE
from app.transmit import enqueue_full_resend
from app.models import (
    Product, Barcode, FtpTask, FtpTaskStatus, ReconciliationLog,
    ReconciliationClassification, SyncSetting,
    PlatformCatalogItem, MappingConflict,
)

LARGE_DELTA_ABSOLUTE = 5
LARGE_DELTA_RATIO = 0.3


# По сколько строк справочника коммитить. Замер 21.09 на боевом масштабе
# (154 232 строки, параллельно писатель, изображающий рассылку): одной
# транзакцией — импорт 36,0 с и ожидание записи до **13,37 с**; порциями по
# тысяче — 26,2 с и 0,43 с. Отказов «database is locked» в замере не было только
# потому, что `busy_timeout` тридцать секунд: тринадцать — опасно близко, и на
# диске помедленнее или справочнике побольше это ровно тот отказ, который у нас
# нигде не перехвачен. Порциями вдобавок БЫСТРЕЕ: большая транзакция сама по себе
# стоит работы.
DICT_COMMIT_EVERY = 1000


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


def _in_flight_adjustment(db: Session, uid_1c: str, snapshot_at: datetime | None = None) -> int:
    """Сколько «в пути» по этому товару НА МОМЕНТ СНИМКА 1С.

    Приём заказа списывает остаток у нас сразу (подтверждение площадки не ждём —
    заказ уходит в перемещение ЦС → склад площадки), а документ в 1С появляется
    только когда 1С обработает задание и пришлёт результат. В промежутке 1С
    показывает единицы, которых у нас уже нет: незавершённые CREATE_MOVEMENT
    увеличивают ожидаемый остаток в 1С, незавершённые CANCEL_MOVEMENT (возврат на
    ЦС — только по нашей отмене) уменьшают.

    `snapshot_at` — время выгрузки, по которой идёт сверка. Задание, закрытое
    ПОСЛЕ снимка, в самом снимке ещё не проведено, значит на момент снимка оно
    тоже было «в пути». Без этой оговорки задание, закрывшееся в промежутке между
    выгрузкой и сверкой (а это 5 минут штатного расписания), выглядело бы как
    приход на склад и вернуло бы уже отгруженные единицы на площадки."""

    barcodes = {b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c == uid_1c).all()}
    if not barcodes:
        return 0

    # pending/sent — задание ещё в работе. timeout — ответа нет, проведён документ
    # или нет, неизвестно. failed — 1С ответила ERROR, документа заведомо НЕТ.
    # Все три считаем «в пути»: 1С показывает эти единицы у себя, а у нас их уже
    # списали. Не считать их — значит вернуть на склад уже проданное и отправить
    # его на площадки второй раз. Ошибка в другую сторону (если документ всё же
    # проведён) даёт недоотправку и лечится сама, как только задание закроется.
    still_open = FtpTask.status.in_([FtpTaskStatus.pending, FtpTaskStatus.sent,
                                     FtpTaskStatus.timeout, FtpTaskStatus.failed])
    if snapshot_at is None:
        was_open = still_open
    else:
        was_open = or_(still_open, and_(FtpTask.completed_at.isnot(None),
                                        FtpTask.completed_at >= snapshot_at))

    open_tasks = db.query(FtpTask).filter(
        FtpTask.barcode.in_(barcodes),
        was_open,
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
    # Нужен не только факт «баркод есть», но и К ЧЕМУ он привязан и КЕМ: догадку
    # автопривязки справочник 1С обязан перебить (см. ниже). Тремя колонками, а
    # не объектами ORM: строк сто пятьдесят четыре тысячи.
    existing_map = {b: (uid, src) for b, uid, src in db.query(
        Barcode.barcode, Barcode.uid_1c, Barcode.source_platform).all()}
    existing_bcs = set(existing_map)

    stats = {"products": 0, "barcodes": 0, "conflicts_cleared": 0, "full": full,
             "repointed_guesses": 0, "recalc_dropped": 0}
    resolved = set()
    # Товары, которым справочник завёл НОВЫЙ баркод. Отметку «актуализирован» у
    # них надо снять: расчёт собирал заказы строго по прежнему набору баркодов,
    # значит продажи по только что привязанному он заведомо не видел, а догнать
    # их нечем — товар числится актуализированным, `catch_up_product` по нему не
    # зовут, живой опрос старый заказ не принесёт, и наружу уходит остаток,
    # завышенный ровно на эти продажи. Правило то же, что у переподвязки,
    # автопривязки по пулу и импорта «Мэппинга»; здесь его не было.
    #
    # Одним запросом в конце, а не `drop_recalc_mark` на строку: полный прогон —
    # сто пятьдесят четыре тысячи строк, и отдельный SELECT товара на каждую
    # новую означал бы ту же беду, ради которой тут заведён `existing_map`.
    # Условие `recalc_done_at IS NOT NULL` несёт обе предосторожности помощника:
    # у товара без расчёта не трогаем НИЧЕГО (иначе пустая строка в
    # `recalc_account_ids` включила бы ступень 2 лестницы там, где она обязана
    # молчать, и на отмеченный кабинет уехал бы ноль), а тем, у кого расчёт был,
    # ставим пустую СТРОКУ, а не NULL.
    newly_linked: set[str] = set()
    seen = 0
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
            existing_map[barcode] = (uid, "1c_dict")
            existing_bcs.add(barcode)
            stats["barcodes"] += 1
            newly_linked.add(uid)
        else:
            known_uid, source = existing_map[barcode]
            if known_uid != uid and source == POOL_GUESS_SOURCE:
                # Привязку, сделанную ДОГАДКОЙ, справочник 1С перебивает — и
                # только её. `pool_match` ставит `catalog_sync`, когда баркод
                # незнаком, но его соседи по карточке площадки ведут к одному
                # товару 1С. Догадка полезная, но она не первичный учёт: 1С
                # ведёт баркод в карточке товара, и если числа разошлись, права
                # 1С. Раньше приём справочника не менял существующие привязки
                # НИКОГДА, поэтому ошибочная догадка жила вечно: заказ по этому
                # баркоду списывался с чужого товара — у него остаток падал зря,
                # а у настоящего оставался завышенным, то есть уезжал наружу.
                # Конфликт при этом был удалён, и разбирать было нечего.
                #
                # Ручную переподвязку (`excel_repoint`) и записи самой 1С не
                # трогаем: первое — осознанное решение человека под отдельной
                # галочкой, второе и так отсюда.
                row = db.query(Barcode).filter(Barcode.barcode == barcode).first()
                if row is not None:
                    row.uid_1c = uid
                    row.source_platform = "1c_dict"
                    existing_map[barcode] = (uid, "1c_dict")
                    # Отметку «актуализирован» снимаем у ОБОИХ товаров: расчёт
                    # собирал заказы по прежнему набору баркодов.
                    for affected in (known_uid, uid):
                        p = db.query(Product).filter(Product.uid_1c == affected).first()
                        if p is not None:
                            p.recalc_done_at = None
                            p.recalc_account_ids = ""
                    log_action(db, "1c_dict", "barcode_guess_corrected",
                               f"{barcode}: {known_uid} -> {uid} (была догадка "
                               f"автопривязки, справочник 1С поправил)")
                    stats["repointed_guesses"] += 1
        resolved.add(barcode)

        # Коммитим ПОРЦИЯМИ, как часовая сверка. Полный прогон справочника — это
        # сто пятьдесят четыре тысячи строк, и одна транзакция на всё держала
        # эксклюзивную блокировку записи всё время импорта: ни рассылка, ни приём
        # заказов, ни веб писать не могли, а за `busy_timeout` в 30 секунд —
        # `database is locked`, нигде не перехваченный. Атомарность тут не нужна
        # и даже вредна: каждая строка независима, импорт идемпотентен по
        # построению (заводим только недостающее), и оборвавшийся на середине
        # прогон доделает следующий, а не откатит сделанное.
        seen += 1
        if seen % DICT_COMMIT_EVERY == 0:
            db.commit()

    db.flush()
    linked = list(newly_linked)
    for i in range(0, len(linked), 500):
        stats["recalc_dropped"] += db.query(Product).filter(
            Product.uid_1c.in_(linked[i:i + 500]),
            Product.recalc_done_at.isnot(None),
        ).update({"recalc_done_at": None, "recalc_account_ids": ""},
                 synchronize_session=False)
        db.commit()

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

# По сколько товаров коммитить сверку. Двести — компромисс: блокировка держится
# доли секунды, а накладные расходы на коммит не становятся заметными.
RECONCILE_COMMIT_EVERY = 200


def run_reconciliation(db: Session, stock_from_1c: dict[str, int],
                       missing_means_zero: bool = False,
                       snapshot_at: datetime | None = None) -> dict:
    """stock_from_1c: {баркод: количество на ЦС} — результат периодической
    выгрузки из старой базы (раздел 8). Раз в час, согласно спецификации.

    `missing_means_zero` — выгрузка является ПОЛНЫМ снимком склада ЦС. Тогда товар,
    которого в снимке нет, физически распродан в ноль: виртуальная таблица
    «ТоварыНаСкладах.Остатки» нулевые позиции не возвращает, и без этого флага такой
    товар не сверялся бы никогда — приложение вечно транслировало бы на площадки
    последнее ненулевое число (прямой оверселл).

    `snapshot_at` — время файла выгрузки. Нужно, чтобы правильно посчитать «в пути»:
    задание, закрытое уже после снимка, в снимке ещё не проведено (см.
    `_in_flight_adjustment`). Без него сверка занижает «в пути» и возвращает на
    склад единицы, которые площадка уже продала."""

    stats = {"normal": 0, "auto_plus": 0, "auto_minus": 0, "needs_review": 0,
             "unmatched_barcodes": 0, "zeroed_missing": 0, "barcode_conflicts": 0}

    # Группируем полученные из 1С количества по uid_1c (может быть несколько
    # баркодов на один товар — считаем максимум, т.к. это один физический остаток)
    uid_to_actual = {}
    conflicting_uids: set[str] = set()
    for barcode, qty in stock_from_1c.items():
        row = db.query(Barcode).filter(Barcode.barcode == barcode).first()
        if row is None:
            stats["unmatched_barcodes"] += 1
            continue
        # Максимум по баркодам одного товара — это один физический остаток. Но
        # начинать максимум с нуля нельзя: отрицательный остаток (пересортица)
        # превращался бы в 0 и в журнале сверки тоже. Весь остальной код
        # специально хранит минус как есть (приём заказа, сверка), на площадку
        # всё равно уходит max(0, …). Поэтому первое значение берём как есть.
        previous = uid_to_actual.get(row.uid_1c)
        if previous is not None and previous != qty:
            # Несколько штрихкодов одного SKU держат ОДИН физический остаток, и
            # 1С отдаёт по ним одно и то же число. Разные числа означают, что
            # один из баркодов привязан к чужому товару, — и максимум тогда
            # берёт ЧУЖОЙ остаток: на площадку уходит больше, чем лежит на
            # складе. Формулу это не меняет (минимум и сумма врут так же, просто
            # в другую сторону, а починка тут одна — мэппинг), но молчать об
            # этом нельзя: предохранитель, сработавший молча, — половина
            # предохранителя. Считаем и отдаём наверх.
            conflicting_uids.add(row.uid_1c)
        uid_to_actual[row.uid_1c] = qty if previous is None else max(previous, qty)

    # Считаем ТОВАРЫ, а не расхождения: разбирать человеку товар, и три
    # разошедшихся баркода на одном товаре — это один разбор, а не два.
    stats["barcode_conflicts"] = len(conflicting_uids)

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

    for processed, (uid_1c, actual_1c) in enumerate(uid_to_actual.items(), start=1):
        # Коммит ПОРЦИЯМИ, а не один в конце. Раньше вся выгрузка — около
        # полутора тысяч товаров — шла одной транзакцией, и всё это время писать
        # в базу не мог никто: ни рассылка (цикл раз в 45 секунд), ни приём
        # заказов, ни оператор в браузере. `busy_timeout` — тридцать секунд, за
        # ним `database is locked`, а этот случай нигде не перехватывается.
        # Атомарность тут не нужна и не нужна была: каждая строка независима, а
        # недосчитанная порция досчитается в следующий час — сверка идемпотентна
        # по построению, она сравнивает текущее состояние со снимком.
        if processed % RECONCILE_COMMIT_EVERY == 1 and processed > 1:
            db.commit()

        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            continue

        in_flight = _in_flight_adjustment(db, uid_1c, snapshot_at)
        expected_1c = product.stock_on_hand + in_flight
        delta = actual_1c - expected_1c

        classification = classify_delta(delta, product.stock_on_hand)

        log = ReconciliationLog(
            uid_1c=uid_1c, python_stock=product.stock_on_hand, in_flight=in_flight,
            expected_1c=expected_1c, actual_1c=actual_1c, delta=delta,
            classification=classification,
            # Совпадение — не расхождение, и разрешать его некому и незачем.
            # Раньше `resolved` ставился ТОЛЬКО при delta != 0, поэтому каждая
            # сходящаяся строка — а это подавляющее большинство, около сорока
            # тысяч в сутки — оставалась «неразрешённой» навсегда. Кнопка
            # «Закрыть старые расхождения» поднимала их все разом одной
            # транзакцией: замер на 250 тысячах дал 3,8 с на загрузку, 10,9 с на
            # обновление и 857 МБ памяти, а к девяностому дню хранения их стало
            # бы около трёх с половиной миллионов. Десять секунд эксклюзивной
            # блокировки записи — это `database is locked` у рассылки и приёма
            # заказов, нигде не перехваченный.
            resolved=(delta == 0),
        )
        db.add(log)
        stats[classification.value] += 1

        if delta != 0:
            # По решению оператора применяем ЛЮБОЕ движение по складу (приход/расход)
            # автоматически, независимо от величины. classify_delta оставлен для
            # журнала (крупные по-прежнему видно как needs_review), но остаток и
            # ручную цифру двигаем всегда.
            #
            # Применяем НЕ сырое actual_1c, а actual_1c − «в пути». 1С показывает
            # склад ЦС до проведения наших незакрытых заданий: заказ списывает
            # остаток у нас сразу, документ в 1С появляется позже. Сырое
            # присваивание вернуло бы уже отгруженные единицы обратно на склад и
            # отправило бы их на площадки — ошибка ровно на сумму незакрытых
            # заданий по товару, то есть максимальная в часы пиковых продаж.
            #
            # Тождество: actual − in_flight = (expected + delta) − in_flight
            #                               = stock + delta,
            # то есть остаток двигается ровно на дельту склада — так же, как
            # ниже двигается transmit_override. Отрицательный результат (склад
            # распродан в магазине глубже, чем мы успели отгрузить) сохраняем:
            # это честная пересортица, на площадки при этом уходит 0.
            new_stock = actual_1c - in_flight
            product.stock_on_hand = new_stock

            # Ручная цифра трансляции (transmit_override) «дышит» вместе со складом:
            # приход прибавляет, расход убавляет — ровно на delta. Заказы в delta НЕ
            # входят (учтены через in_flight и уже двигают override в момент заказа),
            # поэтому задвоения нет. Клампа в ноль нет намеренно: он делал движения
            # необратимыми и цифра дрейфовала вверх. На площадку уходит max(0, …).
            if product.transmit_override is not None:
                product.transmit_override = product.transmit_override + delta

            log.resolved = True

            # Рассылаем актуальное значение на включённые кабинеты (дефицит — риск
            # оверселла, поэтому сразу, не ждём батч-цикл). Если задан override,
            # dispatch отправит именно его (см. _quantity_to_send).
            #
            # ЧЕРЕЗ `enqueue_full_resend`, а не прямым `db.add`. Раньше было
            # прямым, и это обходило ОБА гейта трансляции: товар с выключенной
            # трансляцией и кабинет, не покрытый расчётом, всё равно попадали в
            # очередь. Дальше `quantity_for_account` честно возвращал по ним 0
            # (ступени 0 и 2 лестницы), и этот ноль уходил на площадку — рассылка
            # нули не пропускает. Для карточки, на которую мы ни разу не
            # отправляли остаток, это не отзыв, а обнуление чужих продаж: ровно
            # то, что случилось 18.09 с Озоном и Kit. Тогда гейты добавили в приём
            # заказа и в саму `enqueue_full_resend`, а путь сверки остался мимо
            # них и продолжал обнулять каждый час.
            enabled_platforms = db.query(SyncSetting).filter(
                SyncSetting.uid_1c == uid_1c, SyncSetting.enabled.is_(True),
            ).all()
            for setting in enabled_platforms:
                enqueue_full_resend(db, uid_1c, setting.account_id, reason="reconciliation")

    db.commit()
    return stats
