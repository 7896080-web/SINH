"""Возвраты: приёмка, статусы, отправка в 1С.

Что здесь важно понять про ОСТАТОК, потому что всё остальное — следствие.

Пока вещь в разборе, её нет в нашем `stock_on_hand` и нет на ЦС у 1С: мы её не
приходовали. Остаток ЦС растёт ровно тогда, когда 1С ответила по заданию, и
растёт обычным путём, часовым снимком. Поэтому:

  * зависшее задание занижает остаток, а не завышает, — то есть ошибается в
    безопасную сторону (недопродажа, не оверселл);
  * статус «возвращён в продажу» ставит ТОЛЬКО ответ 1С. Кнопка, объявляющая
    остаток выросшим раньше 1С, — ровно тот класс дефекта, ради которого весь
    этот проект и переписывался.

**Но «её нет нигде» — неправда, и это стоило отдельного разбора.** В 1С единица
ЧИСЛИТСЯ НА СКЛАДЕ ПЛОЩАДКИ: туда её увезло перемещение при приёме заказа.
Наш `stock_on_hand` описывает только ЦС, поэтому оттуда её не видно — а она
есть. Отсюда следствие, которое сначала было записано здесь ПРЯМО НАОБОРОТ
(«утилизация в 1С не идёт вовсе, списывать нечего»): выбросив вещь физически и
не проведя документов, мы оставляем её на складе площадки НАВСЕГДА, и учёт
расходится со складом накопительно, по штуке на каждую утилизацию. Само это не
сойдётся ни сверкой, ни выгрузкой.

Поэтому утилизация идёт в 1С своей командой `SCRAP_RETURN`, и 1С делает по ней
ДВА документа одной транзакцией: возврат «склад площадки → ЦС» и списание с ЦС.
Статус «утилизирован» ставит, как и «возвращён в продажу», ТОЛЬКО ответ 1С.

**Команда своя — `RETURN_TO_STOCK`, и это не вкусовщина.** Сверка считает «в
пути» так: `CREATE_MOVEMENT` со знаком плюс, `CANCEL_MOVEMENT` со знаком минус,
остальные команды — ноль (`reconciliation._in_flight_adjustment`). Оформи мы
возврат как `CREATE_MOVEMENT` с перевёрнутыми складами — а соблазн есть, в 1С
тогда менять нечего, — сверка посчитала бы его уходом с ЦС и ЗАНИЗИЛА остаток
вдвое против правды. Ноль же для возврата ровно верен, см. выше.

**Автоповтора у возвратов нет, и это тоже следствие:** `repost_stuck_movements`
берёт только `CREATE_MOVEMENT`. Значит идемпотентность на стороне 1С для
возвратов не требуется — повторять их никто не станет, — а зависшее задание
уходит в ручной разбор по общему правилу.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Barcode, FtpTask, FtpTaskStatus, Platform, Product, ReturnItem, ReturnItemLog,
    ReturnStatus, ScrapReason, RETURN_BY_1C, RETURN_TRANSITIONS,
)
from app.timeutils import now_utc

# Имена команд в файле задания для 1С.
RETURN_COMMAND = "RETURN_TO_STOCK"
# Утилизация — ОТДЕЛЬНАЯ команда, и делает она в 1С ДВА документа в одной
# транзакции: возврат «склад площадки → ЦС» и списание с ЦС («Списание товаров»,
# хоз. операция «Утилизация Брака»). Порознь их слать нельзя: между документами
# остаток ЦС вырастет на единицу, и часовая выгрузка, попав в этот промежуток,
# увезёт вещь на площадки как доступную — а она уже в мусоре. Промежуточного
# состояния не должно существовать вовсе.
SCRAP_COMMAND = "SCRAP_RETURN"

# Команды возвратов, которые в «в пути» НЕ участвуют. Обе дают ноль, и обе по
# своей причине: у возврата вещи нет ни у нас, ни в 1С, пока та не ответила; у
# утилизации два документа гасят друг друга на ЦС (+1 и −1), а уходит единица со
# склада ПЛОЩАДКИ, которого наш `stock_on_hand` не описывает вовсе.
RETURN_COMMANDS = (RETURN_COMMAND, SCRAP_COMMAND)

# Куда возвращается вещь. Приёмная сторона одна на все площадки — центральный
# склад; исходная берётся по площадке из карты складов планировщика.
TARGET_WAREHOUSE = "ЦС Склад"

# Дребезг сканера: то же самое за эти секунды — точно один жест, не спрашиваем.
SCANNER_BOUNCE = timedelta(seconds=2)

# Дольше — уже вопрос: два одинаковых возврата подряд это норма, и молча
# отбросить второй значит потерять настоящую вещь.
SAME_BARCODE_ASK = timedelta(minutes=30)

LABEL_RE = re.compile(r"^RET-0*(\d+)$", re.IGNORECASE)

# Тренировочный режим. ОДИН НА УСТАНОВКУ, а не на браузерную сессию, и это
# главное решение здесь: склад работает под ОБЩЕЙ учётной записью, и режим,
# живущий в сессии, означал бы, что один человек тренируется, а второй в
# соседнем окне принимает настоящие возвраты, считая, что тоже тренируется, —
# или наоборот. Про режим должна знать УСТАНОВКА, и полоса на экране обязана
# кричать о нём: неверное представление о том, в каком ты режиме, и есть вся
# опасность этой затеи.
TEST_MODE_SETTING = "RETURNS_TEST_MODE"


class ReturnError(Exception):
    """Отказ, который надо показать человеку словами."""


def label_number(item: ReturnItem) -> str:
    """Номер, который печатается на наклейке и сканируется с неё."""
    return f"RET-{item.id}"


def parse_label(text: str) -> int | None:
    """Номер возврата из отсканированного текста, иначе None.

    Одно поле ввода на всё: товарный баркод заводит вещь, `RET-…` открывает
    существующую. Так снимается целый класс ошибок «отсканировал не в то поле».
    """
    m = LABEL_RE.match((text or "").strip())
    return int(m.group(1)) if m else None


def find_product(db: Session, barcode: str) -> Product | None:
    link = db.query(Barcode).filter(Barcode.barcode == barcode).first()
    if link is None:
        return None
    return db.query(Product).filter(Product.uid_1c == link.uid_1c).first()


def recent_same_barcode(db: Session, barcode: str, platform: Platform) -> ReturnItem | None:
    """Недавний возврат того же баркода — чтобы спросить про повторный скан."""
    return (db.query(ReturnItem)
            .filter(ReturnItem.barcode == barcode,
                    ReturnItem.platform == platform,
                    ReturnItem.created_at >= now_utc() - SAME_BARCODE_ASK)
            .order_by(ReturnItem.created_at.desc()).first())


def is_scanner_bounce(item: ReturnItem | None) -> bool:
    return item is not None and now_utc() - item.created_at <= SCANNER_BOUNCE


def accept(db: Session, barcode: str, platform: Platform,
           is_test: bool = False) -> ReturnItem:
    """Принять вещь. Неопознанный баркод приёмке НЕ мешает.

    Вещь физически существует независимо от нашего мэппинга, и отказать в скане
    значит заставить человека отложить её в сторону и забыть. Товар 1С
    определится позже — строка видна отдельным фильтром.
    """
    barcode = (barcode or "").strip()
    if not barcode:
        raise ReturnError("Пустой баркод")

    product = find_product(db, barcode)
    item = ReturnItem(barcode=barcode, platform=platform,
                      uid_1c=product.uid_1c if product else None,
                      status=ReturnStatus.accepted, status_changed_at=now_utc(),
                      is_test=is_test)
    db.add(item)
    db.flush()          # нужен id: он же номер наклейки
    db.add(ReturnItemLog(return_id=item.id, from_status=None,
                         to_status=ReturnStatus.accepted,
                         note="принят на складе"))
    return item


def cancel_acceptance(db: Session, item: ReturnItem) -> None:
    """Отменить приёмку. Только пока вещь никуда не двинулась.

    Дальше — только отмена решения с причиной: запись уже что-то утверждает о
    физическом мире, и стереть её молча значит соврать.
    """
    if item.status is not ReturnStatus.accepted:
        raise ReturnError("Отменить приёмку можно, только пока вещь не тронута")
    db.delete(item)


def can_change(item: ReturnItem, to_status: ReturnStatus) -> bool:
    return to_status in RETURN_TRANSITIONS.get(item.status, ())


def change_status(db: Session, item: ReturnItem, to_status: ReturnStatus,
                  note: str = "", scrap_reason: ScrapReason | None = None) -> None:
    """Перевести вещь. Таблица переходов — единственный источник правды.

    Из «ждём 1С» руками не выйти вовсе (`RETURN_TRANSITIONS` пуста), и оба её
    выхода ставит `apply_1c_result`. Нажать «возвращён в продажу», пока задание
    в пути, значило бы заявить, что остаток вырос, хотя 1С этого не говорила.
    """
    if to_status in RETURN_BY_1C:
        raise ReturnError("Этот статус ставит только ответ 1С")
    if not can_change(item, to_status):
        raise ReturnError(f"Из «{RETURN_LABELS[item.status]}» нельзя "
                          f"в «{RETURN_LABELS[to_status]}»")
    _move(db, item, to_status, note)


def _move(db: Session, item: ReturnItem, to_status: ReturnStatus, note: str) -> None:
    db.add(ReturnItemLog(return_id=item.id, from_status=item.status,
                         to_status=to_status, note=(note or "")[:255]))
    item.status = to_status
    item.status_changed_at = now_utc()


def source_warehouse(platform: Platform) -> str:
    """Склад площадки — ТОТ ЖЕ, на который уезжает товар при приёме заказа.

    Карта живёт у планировщика и заведена по ПЛОЩАДКЕ, а не по кабинету: ИП к
    складу отношения не имеет. Импорт отложенный: тянуть планировщик со всем
    APScheduler в веб-запрос ради двух словарей незачем, а копия карты здесь
    однажды разошлась бы с той, по которой уходят заказы.
    """
    from app.workers.scheduler import PENDING_WAREHOUSE_NAME

    return PENDING_WAREHOUSE_NAME[platform]


def send_to_1c(db: Session, item: ReturnItem) -> FtpTask:
    """Решение «вернуть в продажу»: задание в 1С и статус «ждём 1С».

    Перемещение зеркально приёму заказа: склад площадки → ЦС. `account_id`
    пуст намеренно — кабинет в документе не участвует, а на приёмке он и
    неизвестен.
    """
    if item.status in RETURN_BY_1C:
        raise ReturnError("Решение по этой вещи уже принято")
    if not can_change(item, ReturnStatus.awaiting_1c):
        raise ReturnError(f"Из «{RETURN_LABELS[item.status]}» нельзя вернуть в продажу")
    if not item.uid_1c:
        # Без товара 1С перемещение проводить не на что: 1С опознаёт строку по
        # баркоду, а этого баркода она не знает.
        raise ReturnError("Товар 1С по этому баркоду не определён — сначала мэппинг")

    task = FtpTask(command=RETURN_COMMAND, barcode=item.barcode,
                   warehouse_from=source_warehouse(item.platform),
                   warehouse_to=TARGET_WAREHOUSE, quantity=1,
                   order_id=label_number(item), account_id=None,
                   platform=item.platform, status=FtpTaskStatus.pending,
                   is_test=item.is_test)
    db.add(task)
    db.flush()
    item.ftp_task_id = task.id
    _move(db, item, ReturnStatus.awaiting_1c,
          f"задание в 1С: {source_warehouse(item.platform)} → {TARGET_WAREHOUSE}")
    return task


def send_scrap_to_1c(db: Session, item: ReturnItem,
                     scrap_reason: ScrapReason) -> FtpTask:
    """Решение «утилизировать»: задание в 1С и статус «ждём 1С: утилизация».

    Долгое время здесь не было ничего: считалось, что вещи в 1С нет, а значит и
    списывать нечего. Это была прямая неправда. При приёме заказа перемещение
    увезло единицу ЦС → склад площадки, то есть в 1С она ЧИСЛИТСЯ ТАМ. Покупатель
    вернул, мы выбросили — и без документов она висит на складе площадки вечно, а
    учёт расходится со складом накопительно, по штуке на каждую утилизацию.
    Поэтому 1С делает ДВА документа, и оба — её работа, не наша отметка.

    Причина обязательна: «утилизировано 40» — число без смысла, а «из них 12
    подмена» — повод для претензии площадке, штука на маркетплейсах обычная и
    дорогая. Ставим её ПРИ ОТПРАВКЕ, а не в терминальном статусе: терминальный
    ставит ответ 1С, и ждать до него значило бы потерять причину при отказе.
    """
    if item.status in RETURN_BY_1C:
        raise ReturnError("Решение по этой вещи уже принято")
    if not can_change(item, ReturnStatus.awaiting_scrap):
        raise ReturnError(f"Из «{RETURN_LABELS[item.status]}» нельзя утилизировать")
    if scrap_reason is None:
        raise ReturnError("У утилизации обязательна причина")
    if not item.uid_1c:
        # Оба документа 1С опознаёт строку по баркоду, а этого баркода она не
        # знает: ни вернуть на ЦС, ни списать с него нечего.
        raise ReturnError("Товар 1С по этому баркоду не определён — сначала мэппинг")

    task = FtpTask(command=SCRAP_COMMAND, barcode=item.barcode,
                   warehouse_from=source_warehouse(item.platform),
                   warehouse_to=TARGET_WAREHOUSE, quantity=1,
                   order_id=label_number(item), account_id=None,
                   platform=item.platform, status=FtpTaskStatus.pending,
                   is_test=item.is_test)
    db.add(task)
    db.flush()
    item.ftp_task_id = task.id
    # Причину пишем СРАЗУ: она уезжает в комментарий документа списания и
    # остаётся у нас, даже если 1С откажет и вещь вернётся в разбор.
    item.scrap_reason = scrap_reason
    _move(db, item, ReturnStatus.awaiting_scrap,
          f"утилизация в 1С, «{SCRAP_OPERATION[scrap_reason]}»: "
          f"{source_warehouse(item.platform)} → {TARGET_WAREHOUSE} → списание")
    return task


def recall_before_send(db: Session, item: ReturnItem) -> None:
    """Отменить отправку, ПОКА задание не уехало.

    Граница честная и жёсткая: `pending` — файл ещё не собран, отмена ничего не
    стоит. `sent` и дальше — задание у 1С, и клик здесь его не вернёт; говорим
    это прямо, а не делаем вид, что отменили.
    """
    if item.status not in AWAITING or item.ftp_task_id is None:
        raise ReturnError("Отменять нечего")
    task = db.query(FtpTask).filter(FtpTask.id == item.ftp_task_id).first()
    if task is None or task.status is not FtpTaskStatus.pending:
        raise ReturnError("Задание уже ушло в 1С — отменить можно только сторно в самой 1С")
    db.delete(task)
    item.ftp_task_id = None
    _move(db, item, ReturnStatus.accepted, "отправка отменена до ухода задания")


def apply_1c_result(db: Session, task: FtpTask, ok: bool) -> None:
    """Ответ 1С двигает вещь. Единственный путь в ЛЮБОЙ терминальный статус.

    Куда именно — решает КОМАНДА задания, а не статус вещи: ожиданий теперь два,
    и спутать их нельзя. Ответ по утилизации, поставивший «возвращён в продажу»,
    объявил бы выброшенную вещь снова продающейся — она уехала бы на площадки, и
    купил бы её живой человек.
    """
    item = db.query(ReturnItem).filter(ReturnItem.ftp_task_id == task.id).first()
    if item is None or item.status not in AWAITING:
        return
    if ok:
        done = (ReturnStatus.scrapped if task.command == SCRAP_COMMAND
                else ReturnStatus.back_to_sale)
    else:
        done = ReturnStatus.rejected_1c
    _move(db, item, done, (task.result_detail or "")[:255])


# Оба ожидания ответа 1С. Список ОДИН на всех, кто спрашивает «в пути ли вещь»:
# отзыв отправки, разбор ответа, подсказки. Перечисли их кто-нибудь у себя — и
# новое ожидание однажды не попало бы в один из списков, молча.
AWAITING = (ReturnStatus.awaiting_1c, ReturnStatus.awaiting_scrap)

RETURN_LABELS = {
    ReturnStatus.accepted: "Принят",
    ReturnStatus.cleaning: "В химчистке",
    ReturnStatus.repack: "На переупаковке",
    ReturnStatus.held: "Отложен",
    ReturnStatus.awaiting_1c: "Ждём 1С: возврат в продажу",
    ReturnStatus.awaiting_scrap: "Ждём 1С: утилизация",
    ReturnStatus.back_to_sale: "Возвращён в продажу",
    ReturnStatus.rejected_1c: "Отказ 1С",
    ReturnStatus.scrapped: "Утилизирован",
}

# Что статус ЗНАЧИТ и что делать дальше. Два разных вопроса, поэтому два поля:
# подпись «Отложен» не говорит человеку ни того, ни другого, а он видит её через
# неделю после того, как сам её поставил, и уже не помнит почему.
#
# Живёт ЗДЕСЬ, рядом с таблицей переходов, а не в шаблоне: подсказка обязана
# называть те же переходы, которые страница действительно даст. Разойдись они —
# текст советовал бы кнопку, которой нет, и это хуже, чем отсутствие подсказки:
# человек ищет её, не находит и решает, что страница сломана.
RETURN_HINTS = {
    ReturnStatus.accepted: (
        "Вещь принята и лежит в разборе. У нас её пока нет в остатке, у 1С — тоже.",
        "Осмотрите: чистая ли, нет ли брака, тот ли товар вернули. "
        "Дальше — в химчистку, на переупаковку, сразу в продажу или в утиль.",
    ),
    ReturnStatus.cleaning: (
        "Уехала в химчистку. Решения по ней ещё нет.",
        "Вернулась чистой — «Вернуть в продажу». Пятно не вышло — «Утилизировать» "
        "с причиной «Износ, следы носки».",
    ),
    ReturnStatus.repack: (
        "Товар годен, испорчена упаковка: нужен новый пакет, бирка, этикетка.",
        "Переупаковали — «Вернуть в продажу».",
    ),
    ReturnStatus.held: (
        "Отложена: посмотрели, решение не приняли. Обычно спорный случай — "
        "подмена, непонятный дефект, нужен чей-то ответ.",
        "Не держите долго: пока вещь здесь, она не продаётся и в остатке её нет. "
        "Разберитесь и выберите продажу или утиль.",
    ),
    ReturnStatus.awaiting_1c: (
        "Решение принято, задание на ВОЗВРАТ ушло в 1С. Вещь войдёт в остаток, "
        "когда 1С ответит, — и только тогда.",
        "Ждать. Руками этот статус не меняется. Передумали — «Отменить отправку», "
        "но это возможно, только пока задание не ушло в 1С.",
    ),
    ReturnStatus.awaiting_scrap: (
        "Решение принято, задание на УТИЛИЗАЦИЮ ушло в 1С. Она сделает два "
        "документа сразу: вернёт вещь со склада площадки на ЦС и спишет её "
        "оттуда — иначе единица висела бы на складе площадки вечно.",
        "Ждать. Руками этот статус не меняется. Вещь можно выбрасывать физически: "
        "учёт догонит ответом 1С. Передумали — «Отменить отправку», но только "
        "пока задание не ушло.",
    ),
    ReturnStatus.back_to_sale: (
        "1С оприходовала вещь на ЦС. Она снова в остатке и уедет на площадки "
        "ближайшей рассылкой.",
        "Ничего. Дело закончено — уберите вещь на склад.",
    ),
    ReturnStatus.rejected_1c: (
        "1С отказала по заданию — по возврату или по утилизации, смотря что "
        "отправляли. Документов в 1С НЕТ: ни вещь не оприходована, ни списание не "
        "проведено. Причина в самой 1С — нет номенклатуры, закрыт период, не тот "
        "склад.",
        "Причина написана в истории внизу. Починили в 1С — отправьте ещё раз, тем "
        "же решением. Не чинится — «Отложить» и спросите администратора.",
    ),
    ReturnStatus.scrapped: (
        "1С провела оба документа: вещь вернулась со склада площадки на ЦС и "
        "списана с него как брак. Учёт сошёлся со складом, на ЦС остаток не "
        "изменился — плюс и минус погасили друг друга.",
        "Ничего. Дело закончено, отменить можно только сторно в самой 1С.",
    ),
}

# Причина утилизации → ХОЗЯЙСТВЕННАЯ ОПЕРАЦИЯ документа списания в 1С.
#
# Это не украшение и не комментарий: хоз. операция определяет, куда лягут
# проводки. Все четыре уже заведены в базе («Списание на затраты»), и попасть в
# них причина должна именно так — текстом в комментарии она осталась бы полем
# для чтения, а «из них 12 подмена» нужно в ОТЧЁТЕ, по которому пишут претензию
# площадке.
#
# Карта живёт ЗДЕСЬ, у нас, а не в обработке 1С, и уезжает наименованием в самой
# строке задания. Обработка про наши причины не знает вовсе — она получает
# наименование и ищет его в справочнике. Разойтись тут нечему: заведи кто-нибудь
# пятую причину, забыв про 1С, — тест ниже не даст.
SCRAP_OPERATION = {
    ScrapReason.defect: "Утилизация Брака",
    ScrapReason.worn: "Утилизация Износ",
    ScrapReason.illiquid: "Утилизация Нелеквид",
    ScrapReason.swapped: "Утилизация Подмены",
}

# Кто числится ОТВЕТСТВЕННЫМ в документе списания 1С.
#
# Реквизит там обязателен по смыслу («Акт списания товаров» печатается с ним), и
# оставь мы его пустым, разбираться в накладной было бы не с кем. Значение живёт
# У НАС, а не в обработке: человек уходит в отпуск и увольняется, а правка
# модуля 1С требует Конфигуратора и переноса вручную. Уезжает оно полем строки
# задания, как и хоз. операция, — обработка про наших людей не знает вовсе.
#
# Умолчание нужно именно здесь, а не в `.env`: свежая установка и установка, где
# настройку не трогали, обязаны работать одинаково, а пустой ответственный — это
# отказ проведения на первой же утилизации.
SCRAP_RESPONSIBLE_SETTING = "SCRAP_RESPONSIBLE"
DEFAULT_SCRAP_RESPONSIBLE = "Руслан"


def scrap_responsible(db: Session) -> str:
    """Имя ответственного для документов списания. Пустым не бывает."""
    from app import settings_store

    return (settings_store.get(db, SCRAP_RESPONSIBLE_SETTING).strip()
            or DEFAULT_SCRAP_RESPONSIBLE)


SCRAP_LABELS = {
    ScrapReason.defect: "Брак",
    ScrapReason.worn: "Износ, следы носки",
    ScrapReason.swapped: "Подмена — вернули не тот товар",
    ScrapReason.illiquid: "Неликвид",
}

# Статусы «в работе»: те, где вещь ещё ждёт человека или 1С. Терминальные в
# сводку не идут — она про то, что надо доделать.
IN_WORK = (ReturnStatus.accepted, ReturnStatus.cleaning, ReturnStatus.repack,
           ReturnStatus.held, ReturnStatus.awaiting_1c, ReturnStatus.awaiting_scrap,
           ReturnStatus.rejected_1c)

# Дело закончено: дальше идти некуда, и только такие вещи однажды чистит
# `retention`. Выводится ДОПОЛНЕНИЕМ к `IN_WORK`, а не из таблицы переходов:
# пустая строка в таблице значит «руками не выйти», а не «выхода нет», и у
# `awaiting_1c` она пуста именно поэтому — его двигает ответ 1С. Выведи мы
# терминальные из таблицы, чистка удаляла бы вещи, ПРЯМО СЕЙЧАС ждущие 1С:
# ответ пришёл бы на запись, которой уже нет. Два списка дополняют друг друга
# по построению, и это закреплено тестом — иначе новый статус не попал бы ни в
# один и не чистился бы никогда, молча.
TERMINAL = tuple(s for s in ReturnStatus if s not in IN_WORK)


# --------------------------------------------------------------- тренировка


def test_mode(db: Session) -> bool:
    """Включён ли тренировочный режим. Спрашивают ВСЕ, кто заводит вещь."""
    from app import settings_store

    return settings_store.get(db, TEST_MODE_SETTING).strip().lower() in (
        "1", "true", "yes", "да", "on")


def set_test_mode(db: Session, on: bool) -> None:
    from app import settings_store

    settings_store.set_value(db, TEST_MODE_SETTING, "1" if on else "0")


def test_items(db: Session) -> list[ReturnItem]:
    """Всё, что завёл тренировочный режим. Отдаём СТРОКАМИ, а не числом: выход
    из режима обязан назвать, сколько именно вещей он сейчас сотрёт, — человек
    мог принять в этом режиме настоящий возврат, и тогда число и есть
    единственный шанс это заметить."""
    return db.query(ReturnItem).filter(ReturnItem.is_test.is_(True)).all()


def test_items_count(db: Session) -> int:
    """Сколько сотрёт выход из режима. Отдельным счётом, а не длиной списка:
    спрашивается на КАЖДОЙ загрузке страницы раздела, а вещи грузить незачем.
    Условие то же самое — разойдись оно с `clear_test_data`, вопрос называл бы
    одно число, а стиралось бы другое."""
    return (db.query(func.count(ReturnItem.id))
            .filter(ReturnItem.is_test.is_(True)).scalar() or 0)


def clear_test_data(db: Session) -> int:
    """Стереть тренировочные вещи вместе с их историей и заданиями 1С.

    Условие ОДНО и то же во всех трёх запросах — `is_test`, — и никаких «заодно
    уберём старое»: единственное, что здесь нельзя сделать ни при каких
    обстоятельствах, это задеть настоящий возврат. Он описывает вещь, лежащую на
    складе, и восстановить его будет неоткуда.

    История и задание удаляются ЯВНО, а не каскадом: чистка по всему проекту
    ходит массовым `DELETE`, а он не поднимает ни каскад ORM, ни
    `ON DELETE CASCADE` — на SQLite внешние ключи по умолчанию вообще не
    проверяются. Здесь объём маленький и можно было бы иначе, но правило одно на
    проект: разойдись они, однажды осиротели бы именно здесь.
    """
    items = test_items(db)
    if not items:
        return 0
    ids = [i.id for i in items]
    task_ids = [i.ftp_task_id for i in items if i.ftp_task_id]

    db.query(ReturnItemLog).filter(ReturnItemLog.return_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(ReturnItem).filter(ReturnItem.id.in_(ids)).delete(
        synchronize_session=False)
    if task_ids:
        # `is_test` спрашиваем ещё раз, а не доверяем ссылке: настоящее задание
        # по ссылке из тренировочной вещи означало бы, что где-то мы уже
        # ошиблись, и удалять его на этом основании нельзя — в 1С по нему,
        # возможно, есть документ.
        db.query(FtpTask).filter(FtpTask.id.in_(task_ids),
                                 FtpTask.is_test.is_(True)).delete(
            synchronize_session=False)
    return len(ids)


def simulate_1c(db: Session, item: ReturnItem, ok: bool) -> None:
    """Ответ 1С понарошку — чтобы тренировка доходила до конца.

    Без него тренировочная вещь навсегда зависала бы в «ждём 1С»: настоящего
    ответа не будет, задание в файл не уедет, — и человек не увидел бы ни
    «возвращён в продажу», ни отказа, то есть ровно тех двух экранов, ради
    которых тренировка и затевается.

    Правило «терминальный статус ставит только ответ 1С» при этом НЕ нарушено:
    статус по-прежнему ставит `apply_1c_result`, просто ответ ему приносит
    кнопка. А вот граница безопасности здесь и проходит — отказ на настоящей
    вещи, тем же правилом, что `_refuse_live_order` на странице «Тестирование».
    Разреши мы это, кнопка объявила бы остаток выросшим, хотя 1С молчала: вещь
    считалась бы возвращённой в продажу, её бы отправили на площадки, а в 1С её
    нет.
    """
    if not item.is_test:
        raise ReturnError("Это НАСТОЯЩИЙ возврат — ответ за 1С подделывать нельзя")
    if item.status is not ReturnStatus.awaiting_1c or item.ftp_task_id is None:
        raise ReturnError("Отвечать не на что: задание в 1С не отправлялось")
    task = db.query(FtpTask).filter(FtpTask.id == item.ftp_task_id).first()
    if task is None:
        raise ReturnError("Задание не найдено")
    if not task.is_test:
        # Сюда попасть нельзя по построению, и именно поэтому проверка нужна:
        # если попали, значит тренировочная вещь родила боевое задание, и
        # подделывать ответ на него — худшее из возможных продолжений.
        raise ReturnError("Задание боевое — ответ за 1С подделывать нельзя")
    task.status = FtpTaskStatus.done if ok else FtpTaskStatus.failed
    task.result_status = "OK" if ok else "ERROR"
    task.result_detail = ("тренировочный ответ: 1С "
                          + ("провела документ" if ok else "отказала"))[:255]
    task.completed_at = now_utc()
    apply_1c_result(db, task, ok)
