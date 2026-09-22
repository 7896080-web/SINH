import logging
from datetime import datetime, timedelta

from app.timeutils import now_utc

from sqlalchemy.orm import Session

from app.transmit import quantity_for_account, ever_transmitted, blocked_by_switch
from app.models import (
    AuditLog, DispatchQueueItem, DispatchStatus, SyncSetting, PlatformAccount, Barcode,
    PlatformCatalogItem, Product,
)
from app.workers.order_poller import _representative_barcode
from app.workers.platform_clients.base import PlatformClient, StockPushItem

logger = logging.getLogger("sync_worker")


def _resolve_push_target(db: Session, uid_1c: str, account_id: int) -> tuple[str, str, str] | None:
    """Идентификаторы товара для отправки остатка на КОНКРЕТНЫЙ кабинет.

    У каждой площадки свой ключ остатка (WB — баркод, Ozon — offer_id/артикул,
    Kit — variant_id). Ищем строку каталога этого кабинета по любому баркоду
    из пула товара 1С (у размер-цвета может быть несколько баркодов) и берём
    её идентификаторы. Возвращает (barcode, external_id, article) или None,
    если у товара вообще нет баркодов.

    Если каталог кабинета не загружен (строки нет) — отдаём только баркод
    (external_id/article пустые); клиент WB отработает верно, Ozon/Kit
    упадут обратно на баркод (заведомо загрузите каталог кабинета)."""
    pool = [b.barcode for b in db.query(Barcode).filter(Barcode.uid_1c == uid_1c).all()]
    if not pool:
        return None
    row = (
        db.query(PlatformCatalogItem)
        .filter(PlatformCatalogItem.account_id == account_id, PlatformCatalogItem.barcode.in_(pool))
        .first()
    )
    if row is not None:
        return row.barcode, row.external_id or "", row.article or ""
    return pool[0], "", ""


def push_identifier(db: Session, uid_1c: str, account_id: int,
                    stock_key: str) -> str:
    """Ключ, КОТОРЫМ эта площадка адресует остаток. Пустая строка — ключа нет.

    Одна функция на всех, и это не вкусовщина. Правило выбора («WB — баркод,
    Ozon — артикул, Kit — variant_id») живёт здесь, потому что по нему
    принимаются ДВА разных решения в разных модулях: рассылка решает, идёт ли
    позиция в запрос вовсе, а загрузка каталога — появился ли у закрытой пары
    ключ, которого не хватало. Повтори второй эту логику у себя — они однажды
    разойдутся, и каталог начнёт поднимать пары, по которым отправлять
    по-прежнему нечем, либо молча не поднимать те, по которым уже можно.
    """
    target = _resolve_push_target(db, uid_1c, account_id)
    if target is None:
        return ""
    barcode, external_id, article = target
    return {"barcode": barcode, "external_id": external_id,
            "article": article}.get(stock_key, barcode) or ""


def _quantity_to_send(db: Session, uid_1c: str, account_id: int, quantity: int) -> int:
    """Сколько уйдёт на площадку. Лестница приоритетов — в app/transmit.py, один
    модуль на рассылку и на интерфейс (раньше копии разошлись, и страница показывала
    не то, что реально уходило). Считается в момент отправки, а не при постановке в
    очередь, чтобы взять самые свежие значения."""
    return quantity_for_account(db, uid_1c, account_id, quantity)


# Сколько раз пробуем отправить одну запись, прежде чем признать сбой
# окончательным, и пауза перед каждой следующей попыткой.
MAX_ATTEMPTS = 5

# Сколько позиций уходит на площадку в ОДНОМ запросе. См. комментарий в
# run_dispatch_cycle: предел у каждой площадки свой, сто проходит везде.
PUSH_BATCH_SIZE = 100
RETRY_BACKOFF_MINUTES = (1, 2, 5, 15)   # после 1-й, 2-й, 3-й и 4-й неудачи


def _retry_delay(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой. `attempts` — сколько их уже было."""
    idx = min(max(attempts, 1), len(RETRY_BACKOFF_MINUTES)) - 1
    return timedelta(minutes=RETRY_BACKOFF_MINUTES[idx])


def _publish_restocked(db: Session, account: PlatformAccount, client: PlatformClient,
                       sent_keys: dict[str, int]) -> dict:
    """Вернуть на витрину карточки, которые площадка спрятала за нулевой остаток.

    Зачем это вообще: у Kit при настройке «скрывать товары с нулевым остатком»
    распроданная карточка переходит в `HIDDEN` и САМА оттуда не возвращается.
    Мы шлём приход, площадка его принимает — а товара на витрине нет, и не
    появится никогда. Остаток передан, продаж нет, и снаружи это выглядит как
    наша поломка.

    Три ограничения, и каждое обязательно:

    Только по явному разрешению кабинета (`publish_hidden_on_stock`). Карточку
    мог спрятать и человек — снял с продажи, спорный товар, не сезон, — а
    статус `HIDDEN` у площадки один на оба случая, отличить их нельзя. Поэтому
    решение «возвращать автоматически» принимает человек один раз по кабинету,
    а не мы за него по каждой карточке.

    Только по ненулевому остатку. Ноль карточку на витрине не удержит, а
    публиковать пустую — значит показать людям товар, которого нет.

    Только по тем, что площадка СЕЙЧАС держит скрытыми. Не смогли спросить —
    не публикуем вовсе: `None` от клиента значит «мы не знаем», и трогать по
    нему чужие статусы нельзя.

    Но НЕ ПУБЛИКУЕМ и МОЛЧИМ — разные вещи, а код делал и то и другое: `if not
    hidden: return 0` одинаково проглатывал «скрытых нет» и «спросить не
    удалось», хотя сам комментарий рядом объявлял различие важным. Следствие у
    второго случая своё и отложенное: остаток на карточку ушёл, площадка его
    приняла, у нас всё зелено — а карточка осталась скрытой, покупателям товара
    не видно, и сама она не вернётся. Второй попытки не будет: публикация идёт
    по ключам, отправленным В ЭТОМ цикле, а следующая отправка по медленному
    размеру случится, когда изменится остаток, то есть могут пройти месяцы.
    Поэтому такой случай уходит в лог предупреждением и в журнал действий —
    записью, которая переживёт ротацию логов.
    """
    if not account.publish_hidden_on_stock:
        return {"published": 0, "unchecked": 0}
    restocked = {key for key, quantity in sent_keys.items() if (quantity or 0) > 0}
    if not restocked:
        return {"published": 0, "unchecked": 0}

    ask = getattr(client, "hidden_stock_keys", None)
    hidden = ask() if callable(ask) else None
    if hidden is None:
        # Спросить не удалось (или список вышел неполным — клиент отвечает тем
        # же `None`, см. `kit.hidden_stock_keys`).
        logger.warning(
            "dispatch: кабинет «%s» — не удалось узнать, какие карточки площадка "
            "держит скрытыми; %d карточек получили остаток и могли остаться "
            "невидимыми для покупателей", account.name, len(restocked))
        db.add(AuditLog(
            actor="system", action="variant_publish_unchecked",
            details=f"{account.name}: площадка не ответила списком скрытых карточек — "
                    f"{len(restocked)} карточек получили ненулевой остаток, и если "
                    f"площадка прячет их за нулевой остаток, они остались скрытыми: "
                    f"товар есть, купить нельзя, само не вернётся",
        ))
        db.commit()
        return {"published": 0, "unchecked": len(restocked)}
    if not hidden:
        return {"published": 0, "unchecked": 0}      # скрытых нет — делать нечего

    published = 0
    for key in sorted(restocked & hidden):
        if client.publish_stock_key(key):
            published += 1
            db.add(AuditLog(
                actor="system", action="variant_published",
                details=f"{account.name}: карточка {key} возвращена на витрину — "
                        f"на неё ушёл остаток {sent_keys.get(key)}",
            ))
        else:
            db.add(AuditLog(
                actor="system", action="variant_publish_failed",
                details=f"{account.name}: карточку {key} вернуть на витрину не удалось — "
                        f"остаток на площадке есть, а товар покупателям не виден",
            ))
    db.commit()
    return {"published": published, "unchecked": 0}


def _dispatch_one_account(db: Session, client: PlatformClient, account: PlatformAccount) -> dict | None:
    """Один цикл рассылки по ОДНОМУ кабинету. None — кабинету в этом цикле нечего
    делать (пауза, нет склада, пустая очередь).

    Вынесено из `run_dispatch_cycle` ради одного: сбой этого кабинета не должен
    останавливать остальные. `with_retry` перевыбрасывает `requests.RequestException`
    (таймаут, обрыв, DNS), а клиенты ловят только `HTTPError` — исключение вылетало
    из всего цикла, и кабинеты, стоящие в списке ПОСЛЕ сбойного, в этом проходе не
    обрабатывались вовсе. Список идёт по id, то есть страдали всегда одни и те же.
    Пока лежит WB, Ozon и Kit не получали остатков совсем, хотя сами исправны:
    остаток у нас списан, площадки продают по старым числам — оверселл.
    """
    pending = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.account_id == account.id,
        DispatchQueueItem.status == DispatchStatus.pending,
        DispatchQueueItem.is_test.is_(False),  # тестовые записи со страницы тестирования — никогда не уходят на площадку
    ).order_by(DispatchQueueItem.created_at.asc()).all()

    if not pending:
        return None

    # Дедупликация: несколько записей на один товар за окно накопления —
    # реально нужно отправить только последнее значение. Берём его среди ВСЕХ
    # ожидающих записей, включая те, что сейчас на паузе после сбоя: иначе
    # отложенная запись пережила бы более новую и позже отправила бы на
    # площадку устаревшее число.
    latest_by_uid = {}
    for item in pending:
        latest_by_uid[item.uid_1c] = item

    now = now_utc()
    push_items = []
    uid_to_items = {}
    # Ключи отправки, уже занятые в этом цикле. У Kit пара товар+склад не
    # может повторяться в одном запросе (`DUPLICATE_ITEM`), и повтор ронял
    # бы ВЕСЬ запрос, а не лишнюю строку.
    keys_in_request: dict[str, str] = {}
    # Чем площадка адресует остаток: WB — баркодом, Ozon — артикулом,
    # Kit — variant_id. Спрашиваем у клиента, а не угадываем здесь.
    stock_key = getattr(client, "stock_key", "barcode")
    for uid_1c, item in latest_by_uid.items():
        if item.next_attempt_at is not None and item.next_attempt_at > now:
            continue          # пауза после сбоя ещё не вышла
        target = _resolve_push_target(db, uid_1c, account.id)
        if target is None:
            item.status = DispatchStatus.error
            item.last_error = "нет баркода для отправки"
            continue
        barcode, external_id, article = target
        # Позиция без идентификатора, которым адресует ЭТА площадка, в
        # запрос не идёт вовсе. Отправить её «баркодом, вдруг поймёт» —
        # значит уронить весь запрос: и Kit, и WB бракуют тело целиком,
        # а не отдельный элемент. 20.09 на бою по кабинету КИТ так не
        # уехало ни одного остатка: 11% позиций были без карточки в
        # каталоге, и каждая пачка падала из-за них.
        identifier = {"barcode": barcode, "external_id": external_id,
                      "article": article}.get(stock_key, barcode)
        # (то же правило, что в `push_identifier` — общий помощник для того и
        #  заведён; здесь идентификаторы уже на руках, второй раз их не ищем)
        if not identifier:
            item.status = DispatchStatus.error
            item.next_attempt_at = None
            item.last_error = ("нет карточки в каталоге кабинета — остаток "
                               "отправить не по чему, сначала мэппинг")
            item.card_missing = True
            continue
        if identifier in keys_in_request:
            # Два товара 1С ведут на одну карточку площадки. Число уедет по
            # первому, но молчать нельзя: это дефект мэппинга, и решать его
            # человеку — списывать продажи будут на разные товары.
            item.status = DispatchStatus.error
            item.next_attempt_at = None
            item.last_error = ("на одну карточку площадки ведут два товара 1С "
                               f"({keys_in_request[identifier]} и {uid_1c}) — "
                               "отправлен первый, мэппинг надо поправить")
            continue
        quantity = _quantity_to_send(db, uid_1c, account.id, item.quantity)
        # Ноль на карточку, которой мы НИ РАЗУ не касались, не отправляем вовсе.
        #
        # Инцидент 18.09 закрыли на ВХОДЕ в очередь: автоматические пути больше не
        # ставят в неё нетранслируемый товар. Но запись, попавшая в очередь ДО
        # того как оператор передумал (поставил галочку кабинета — снял её
        # раньше, чем отработал 45-секундный цикл; кабинет был на паузе, в
        # backoff, неактивен), доживает до отправки — и лестница отдаёт по ней
        # ноль, который уезжает наружу. `should_withdraw` в этот момент честно
        # говорит «отзывать нечего», а рассылка в том же цикле обнуляет живую
        # чужую карточку. Различие «осознанно отзываем» против «никогда не
        # отправляли» соблюдалось на входе и терялось на выходе.
        #
        # ТОЛЬКО ноль от выключателя. Ноль от расчёта (распродано, бронь съела
        # остаток, порог кабинета выше доступного) уходить обязан и первым
        # сообщением тоже: трансляция включена и кабинет отмечен — карточку мы
        # сознательно взяли под управление, и промолчать значит оставить её
        # торговать по чужому числу. См. `transmit.blocked_by_switch`.
        if (quantity == 0 and blocked_by_switch(db, uid_1c, account.id)
                and not ever_transmitted(db, uid_1c, account.id)):
            item.status = DispatchStatus.sent
            item.sent_at = None            # на площадку не уходило — не отправка
            item.sent_quantity = None
            item.next_attempt_at = None
            item.last_error = ("ноль не отправлен: на этот кабинет мы ни разу не "
                               "посылали непустой остаток, отзывать нечего")
            continue
        keys_in_request[identifier] = uid_1c
        # Фиксируем ИМЕННО ТО число, которое уходит на площадку. `item.quantity`
        # для этого не годится: там исходный остаток, а не итог лестницы.
        item.sent_quantity = quantity
        # И идентификатор, под которым оно уходит. У товара бывает несколько
        # баркодов, выбор делает `_resolve_push_target` прямо здесь — без
        # записи восстановить ключ по базе потом невозможно. 19.09 разбор
        # «почему на WB ноль» из-за этого занял час: число знали, sku нет.
        item.sent_sku = identifier
        push_items.append(StockPushItem(
            barcode=barcode, quantity=quantity, external_id=external_id, article=article,
        ))
        uid_to_items[barcode] = item

    # Пачками, а не всё разом. Каждая площадка ограничивает размер одного
    # запроса остатков, и предел у всех разный (у Ozon он самый тесный).
    # Сто — осторожное значение, которое проходит везде: ошибка в меньшую
    # сторону стоит лишнего запроса, в большую — отказа ВСЕЙ пачки.
    #
    # При обычной работе очередь за цикл короткая и пачка выходит одна. Но
    # массовая переотправка (`enqueue_resend_all`) кладёт в очередь сразу все
    # транслируемые товары, и без деления это был бы один запрос на столько
    # позиций, сколько их есть. Делим здесь, а не в клиентах: запрос собирает
    # рассылка, и предел должен соблюдаться в одном месте.
    result = {"ok": [], "errors": []}
    for start in range(0, len(push_items), PUSH_BATCH_SIZE):
        part = client.push_stock(account.warehouse_id,
                                 push_items[start:start + PUSH_BATCH_SIZE])
        # Складываем, а не заменяем: отказ одной пачки не должен отменять
        # успех остальных — иначе одна сбойная позиция вернула бы в очередь
        # весь каталог и площадка получила бы его заново следующим циклом.
        result["ok"] += list(part.get("ok", []))
        result["errors"] += list(part.get("errors", []))

    ok_set = set(result.get("ok", []))
    errors_text = str(result.get("errors"))[:400]
    # Ошибки, которые повтором не лечатся: площадка сказала про КОНКРЕТНЫЙ
    # sku, что такого у неё на складе нет. Пять попыток с нарастающей паузой
    # тут не помогут — ответ будет тот же, — зато оттянут на полчаса момент,
    # когда человек узнает, что товар отмечен для кабинета, где его карточки
    # не существует. Поэтому закрываем сразу и с внятным текстом.
    terminal = {str(e.get("sku")): str(e.get("detail") or "")
                for e in result.get("errors", [])
                if isinstance(e, dict) and e.get("terminal") and e.get("sku")}
    # «Карточки нет» — отдельным признаком, а не по тексту: у каждой площадки
    # свои слова, а у Ozon они ещё и по-английски. См. `DispatchQueueItem.card_missing`.
    no_card = {str(e.get("sku")) for e in result.get("errors", [])
               if isinstance(e, dict) and e.get("card_missing") and e.get("sku")}
    retried = 0
    for barcode, item in uid_to_items.items():
        item.attempts += 1
        # `terminal` РАНЬШЕ `ok_set`: площадка, назвавшая позицию виновной, не
        # может одновременно её принять. Раньше порядок был обратным, и клиент,
        # вернувший баркод в обоих списках, закрывал запись как успешную отправку.
        if barcode in terminal:
            item.status = DispatchStatus.error
            item.next_attempt_at = None
            item.last_error = terminal[barcode]
            item.card_missing = barcode in no_card
        elif barcode in ok_set:
            item.status = DispatchStatus.sent
            item.sent_at = now_utc()
            item.next_attempt_at = None
            if (item.sent_quantity or 0) > 0:
                # Память о том, что на эту пару уходил непустой остаток, живёт на
                # самой паре, а не в очереди: очередь чистится по сроку, и вместе
                # с ней исчезала бы возможность отозвать остаток. См.
                # `SyncSetting.last_nonzero_sent_at` и `transmit.ever_transmitted`.
                setting = db.query(SyncSetting).filter(
                    SyncSetting.uid_1c == item.uid_1c,
                    SyncSetting.account_id == account.id,
                ).first()
                if setting is not None:
                    setting.last_nonzero_sent_at = item.sent_at
        elif item.attempts < MAX_ATTEMPTS:
            # Сбой не окончательный: пробуем ещё, с паузой. Остаток уже списан
            # у нас — если не дослать его на площадку, она продаст то, чего нет.
            item.status = DispatchStatus.pending
            item.next_attempt_at = now_utc() + _retry_delay(item.attempts)
            item.last_error = f"попытка {item.attempts} из {MAX_ATTEMPTS}: {errors_text}"
            retried += 1
        else:
            item.status = DispatchStatus.error
            item.last_error = f"не отправлено за {item.attempts} попыток: {errors_text}"

    # Все элементы очереди по товару, кроме самого свежего — считаем
    # поглощёнными (не отправляем устаревшие промежуточные значения)
    for item in pending:
        if item.uid_1c not in latest_by_uid or latest_by_uid[item.uid_1c].id != item.id:
            item.status = DispatchStatus.sent
            item.last_error = "поглощено более новым изменением в этом цикле"

    db.commit()

    # Ненулевой остаток на скрытую карточку — повод вернуть её на витрину.
    # Считаем по ключам отправки, а не по баркодам: публикуется карточка
    # площадки, и адресуется она тем же идентификатором, которым ушёл
    # остаток.
    sent_keys = {item.sent_sku: item.sent_quantity
                 for barcode, item in uid_to_items.items()
                 if barcode in ok_set and item.sent_sku}
    publish = _publish_restocked(db, account, client, sent_keys)

    return {
        "sent": len(ok_set), "errors": len(result.get("errors", [])),
        "queued": len(pending), "retry": retried,
        "published": publish["published"], "unchecked": publish["unchecked"],
    }



def run_dispatch_cycle(db: Session, clients: dict, active_accounts: list[PlatformAccount] | None = None) -> dict:
    """Раз в 30-60 секунд (см. планировщик): забирает накопленную очередь по
    каждому активному кабинету, схлопывает по товару (если за цикл пришло
    несколько изменений — берём только последнее), применяет минимальный
    порог, отправляет батчем.

    Сбой отправки НЕ терминален: запись остаётся `pending` и повторяется с
    нарастающей паузой, пока попытки не исчерпаны (`MAX_ATTEMPTS`). Раньше
    первая же ошибка площадки ставила `error`, и запись не возвращалась в работу
    никогда: остаток был списан у нас, а площадка о нём не узнавала до
    следующего события по товару — то есть продолжала продавать то, чего нет."""

    if active_accounts is None:
        active_accounts = list(db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all())

    stats = {}

    for account in active_accounts:
        client: PlatformClient | None = clients.get(account.id)
        # dispatch_enabled=False — ручная пауза трансляции на этот кабинет:
        # остатки на площадке не трогаем (очередь копится, уйдёт при включении).
        if client is None or not account.warehouse_id or not account.dispatch_enabled:
            continue
        try:
            account_stats = _dispatch_one_account(db, client, account)
        except Exception as e:                  # noqa: BLE001 — причина уходит в статистику
            # Откат обязателен: без него сломанная сессия утащила бы за собой и
            # следующие кабинеты. Записи этого кабинета остаются `pending` без
            # сожжённой попытки и повторятся следующим циклом.
            db.rollback()
            logger.exception("dispatch: кабинет «%s» — сбой цикла", account.name)
            stats[account.name] = {"sent": 0, "errors": 0, "queued": 0, "retry": 0,
                                   "published": 0, "unchecked": 0,
                                   "failed": f"{type(e).__name__}: {e}"}
            continue
        if account_stats is not None:
            stats[account.name] = account_stats

    return stats
