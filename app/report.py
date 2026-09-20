"""Отчёт о расхождениях: что протухло, что зависло, что накопилось.

Зачем он есть. Каждый серьёзный дефект сентября был ВИДЕН В ДАННЫХ за часы до
того, как его нашли: каталог Kit лежал пятидневной давности, двенадцать заданий
1С висели в `timeout`, аномалии копились сотнями. Не хватало не данных, а того,
кто на них посмотрит. Поэтому отчёт собирается сам (`job_discrepancy_report`,
раз в час), пишется в лог и показывается на «Диагностике» и на своей странице.

Главное правило здесь одно: **каждая находка называет не то, что случилось, а
то, чем это кончится, если не трогать.** «12 заданий в статусе timeout» человек
пролистывает; «остаток по 11 товарам занижен на 12 единиц, наружу уходит меньше,
чем есть» — нет. Если для новой проверки такое следствие не формулируется, это
скорее всего не расхождение, а просто число, и ему здесь не место.

Второе правило: отчёт ТОЛЬКО ЧИТАЕТ. Он не чинит, не закрывает, не отправляет
ничего наружу — иначе к нему пришлось бы относиться как к боевому пути и бояться
его запускать. Все запросы — счётчики и минимумы дат, тестовые записи
(`is_test=True`) исключены везде.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Запись без времени создания (данные до появления колонки) не должна
# выигрывать у свежих — считаем её самой старой.
_OLDEST = datetime.min

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models import (
    AnomalyReason, AnomalyStatus, Barcode, DispatchQueueItem, DispatchStatus,
    FtpTask, FtpTaskStatus, MappingConflict, Platform, PlatformAccount,
    PlatformCatalogItem, OrderProcessStatus, ProcessedOrder, Product,
    ReconciliationClassification, ReconciliationLog, StockDateSnapshot,
    StockDateStatus, SyncAnomaly, SyncSetting, WorkerHeartbeat,
)
from app.timeutils import now_utc

# Уровни. Разделены не по громкости, а по тому, чем грозит бездействие:
#   critical — прямо сейчас теряются деньги или уходит неверный остаток;
#   warning  — расхождение копится и станет critical, если не трогать;
# «всё в порядке» отдельным уровнем не является: пустой отчёт и есть порядок.
CRITICAL = "critical"
WARNING = "warning"
LEVEL_ORDER = {CRITICAL: 0, WARNING: 1}

# Пороги. Каждый — из наблюдавшегося на бою поведения, а не из общих соображений.
#
# Каталог кабинета выгружается раз в сутки (`CATALOG_POLL_INTERVAL_HOURS`), так
# что двое суток — это пропущенная выгрузка плюс запас на перезапуски воркера.
CATALOG_STALE = timedelta(days=2)
# Сверка остатка ЦС просит выгрузку раз в час. Три часа — три пропуска подряд;
# один может быть нормальным (1С отвечает по своему расписанию, раз в 10 минут).
RECONCILIATION_STALE = timedelta(hours=3)
# Запись рассылки живёт секунды: цикл идёт раз в 45 секунд. Полчаса в `pending` —
# это не «ещё не дошли руки», а остановившаяся рассылка.
DISPATCH_STUCK = timedelta(minutes=30)
# Задание в 1С: обработка запускается раз в 10 минут, час — шесть пропусков.
FTP_STUCK = timedelta(hours=1)
# Заявка на срез остатков на дату: 1С отвечает в пределах часа.
STOCK_DATE_STUCK = timedelta(hours=3)
# Аномалия, которой больше трёх суток, уже не «разберём на днях».
ANOMALY_OLD = timedelta(days=3)


@dataclass
class Finding:
    """Одно расхождение. `consequence` обязателен — см. заголовок модуля."""
    key: str                    # стабильный идентификатор проверки (для логов и тестов)
    level: str
    title: str
    consequence: str
    count: int = 0
    link: str = ""              # куда идти разбираться
    details: list[str] = field(default_factory=list)


def _age(moment) -> str:
    """Человеческий возраст отметки времени: «3 ч», «5 дн»."""
    if moment is None:
        return "никогда"
    delta = now_utc() - moment
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} мин"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} ч"
    return f"{delta.days} дн"


# --------------------------------------------------------------- сами проверки
# Каждая принимает сессию и возвращает Finding или None. Порядок объявления
# значения не имеет — итоговый список сортируется по уровню.


# По этому признаку отличаем «площадка не знает такого sku» от остальных сбоев
# отправки. Следствия у них РАЗНЫЕ, и мешать их в одну находку нельзя: там, где
# карточки нет, продавать нечего и оверселла не будет, а человеку надо не
# чинить связь, а решить судьбу самой пары товар+кабинет.
# Окно свежести для расхождений сверки: сверка идёт раз в час, сутки дают
# запас на ночь и выходные и при этом не тащат в отчёт прошлую неделю.
RECONCILIATION_WINDOW = timedelta(hours=24)

UNKNOWN_SKU_MARK = "площадка не знает этот sku"
# Второй способ сказать то же самое: карточки этого товара в кабинете нет, и
# рассылка это увидела ДО запроса — по отсутствию идентификатора, которым
# адресует площадка. Следствие то же, что у неизвестного sku, поэтому и находка
# та же: чинить надо мэппинг, а не связь.
NO_CARD_MARK = "нет карточки в каталоге кабинета"
CARD_MISSING_MARKS = (UNKNOWN_SKU_MARK, NO_CARD_MARK)


def _error_gist(text: str | None, limit: int = 160) -> str:
    """Оставить от ошибки то, по чему её можно разобрать.

    21.09 оператор увидел в отчёте ровно это и спросил, как с этим быть:

        2403 · Конко Джемпер … · КИТ — [{'detail': '400 Client Error: Bad
        Request for url: https://api.kit.yandex.net/v1/variants

    Причина обрывается на полуслове, и не потому, что её нет: ответ площадки
    рассылка сохраняет целиком (`kit._push_stock_chunk` кладёт тело в `detail`).
    Съедала её обвязка — «не отправлено за 5 попыток», питоновский repr списка
    словарей, слова «Client Error: Bad Request» и полный адрес ручки. На сам
    ответ площадки, единственное, что тут имеет смысл, не оставалось ни символа.

    Чистим по порядку: счётчик попыток, скобки repr, боилерплейт requests. Код
    ответа сохраняем — 400 и 409 у площадок значат разное. Адрес ручки режем по
    первому «двоеточие с пробелом»: в URL такого сочетания не бывает, а тело
    ответа начинается ровно после него.
    """
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"^(не отправлено за \d+ попыт\w+|попытка \d+ из \d+)\s*:\s*", "", text)
    text = re.sub(r"^\[?\{?\s*'?detail'?\s*:\s*", "", text)
    text = text.strip().strip("[]{}'\"")

    match = re.match(r"^(\d{3})\s+\w+ Error:.*?for url:\s*(.*)$", text, re.S)
    if match:
        code, rest = match.group(1), match.group(2).strip()
        # URL и тело разделены «: » — внутри самого адреса его быть не может
        # (после «https:» идут слэши, а не пробел).
        body = rest.split(": ", 1)[1].strip() if ": " in rest else ""
        text = f"{code}: {body}" if body else f"{code}, ответ пустой ({rest})"
    return text[:limit].strip()


def _describe_pairs(db: Session, rows: list[DispatchQueueItem],
                    limit: int = 10) -> list[str]:
    """Строки находки в виде, пригодном для разбора: артикул, название, кабинет.

    20.09 на бою находка «площадка не знает наш sku» перечисляла внутренние
    идентификаторы вида `051ce509-a048-11ef-…`: в базе по ним всё находится, а
    человеку, который идёт с этим списком в кабинет площадки, они не говорят
    ничего. Находка обязана называть товар так, как его называют люди.

    Один запрос на всю пачку, а не по строке: отчёт собирается раз в час и на
    каждой странице, и N+1 здесь стоил бы секунд на каталоге в 152 тысячи SKU.
    """
    rows = rows[:limit]
    uids = {r.uid_1c for r in rows}
    products = {p.uid_1c: p for p in
                db.query(Product).filter(Product.uid_1c.in_(uids)).all()} if uids else {}
    names = {a.id: a.name for a in db.query(PlatformAccount).all()}

    out = []
    for r in rows:
        product = products.get(r.uid_1c)
        article = (product.article if product else "") or r.uid_1c
        name = (product.name if product else "") or ""
        cabinet = names.get(r.account_id, "")
        line = " · ".join(x for x in (article, name[:45], cabinet) if x)
        if r.sent_sku:
            line += f" (sku {r.sent_sku})"
        out.append(line)
    return out


def current_dispatch_errors(db: Session, account_id: int | None = None) -> list[DispatchQueueItem]:
    """Отказы рассылки, описывающие ТЕКУЩЕЕ состояние пар товар+кабинет.

    Вынесено наружу, чтобы «Диагностика» считала ровно то же, что показывает
    отчёт. 20.09 на бою они разошлись: отчёт сказал «1 запись», а счётчик
    кабинета — «751», потому что считал все строки в `error` за всё время,
    включая мёртвые от уже починенного дефекта. Две страницы, противоречащие
    друг другу, хуже одной неточной: верить перестают обеим.
    """
    query = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.status == DispatchStatus.error,
        DispatchQueueItem.is_test.is_(False),
    )
    if account_id is not None:
        query = query.filter(DispatchQueueItem.account_id == account_id)
    latest = latest_queue_ids(db)
    return [r for r in query.all() if r.id in latest]


def latest_queue_ids(db: Session) -> set[int]:
    """Id последних записей очереди по каждой паре товар+кабинет.

    Очередь событийная: каждая новая запись по паре отменяет смысл всех
    предыдущих — отправлять будут её. Поэтому расхождение описывает ПОСЛЕДНЯЯ
    запись пары, а не каждая историческая попытка.

    20.09 на бою это стало видно наглядно. После починки Kit осталось 659
    записей, перекрытых успешной отправкой, и ещё 100 — от товаров, у которых
    карточки в кабинете нет вовсе. Вторые успехом не перекроются НИКОГДА, и
    отчёт вечно писал бы по ним «площадка продаёт то, чего нет», хотя текущее
    состояние этих пар уже сказано свежей записью: «нет карточки, разбор
    мэппинга». Вечно красный отчёт оператор пролистывает не читая.

    ОДИН запрос на всю очередь, а не запрос на строку. Сначала было наоборот, и
    это стоило дорого: на бою в очереди 864 записи в `error`, то есть 864
    запроса на каждую сборку отчёта — а его собирают страница «Расхождений»
    (сама, раз в две минуты), сводка на «Диагностике», счётчик КАЖДОГО кабинета
    и воркер раз в час. Три колонки по всей очереди читаются за миллисекунды.
    """
    best: dict[tuple[str, int], tuple] = {}
    rows = db.query(
        DispatchQueueItem.id, DispatchQueueItem.uid_1c,
        DispatchQueueItem.account_id, DispatchQueueItem.created_at,
    ).filter(DispatchQueueItem.is_test.is_(False)).all()

    for row_id, uid_1c, account_id, created_at in rows:
        key = (uid_1c, account_id)
        # При равном времени решает id, иначе две записи одной секунды погасили
        # бы друг друга и пара исчезла бы из отчёта совсем.
        mark = (created_at or _OLDEST, row_id)
        if key not in best or mark > best[key]:
            best[key] = mark
    return {mark[1] for mark in best.values()}


def _q_unknown_sku(db: Session) -> list[DispatchQueueItem]:
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.status == DispatchStatus.error,
        DispatchQueueItem.is_test.is_(False),
        or_(*[DispatchQueueItem.last_error.like(f"%{mark}%")
              for mark in CARD_MISSING_MARKS]),
    ).all()
    # Пара товар+кабинет считается один раз: после двух прогонов массовой
    # переотправки по одному и тому же нерешённому товару лежит две записи, а
    # разбирать человеку нечего дважды — это одна неразрешённая пара.
    latest = latest_queue_ids(db)
    return [r for r in rows if r.id in latest]


def _check_unknown_sku(db: Session) -> Finding | None:
    """Товар отмечен для кабинета, а карточки на его складе нет.

    20.09 на бою: один такой баркод в пачке из ста ронял ВЕСЬ запрос ответом
    `409 NotFound`, и девяносто девять живых карточек не получали остаток.
    Теперь виновники вынимаются из запроса, остальное уезжает — но сама пара
    остаётся неразрешённой, и решить её может только человек: либо карточка на
    площадке появится, либо галочку с кабинета надо снять.
    """
    rows = _q_unknown_sku(db)
    if not rows:
        return None
    return Finding(
        key="unknown_sku", level=WARNING,
        title=f"Площадка не знает наш sku: {len(rows)} позиций",
        consequence="Товар отмечен для кабинета, где его карточки на складе нет. "
                    "Остаток туда не уедет никогда — ни сейчас, ни после повторов. "
                    "Оверселла тут не будет (продавать нечего), но пара висит "
                    "нерешённой: либо карточку заводить, либо снимать галочку.",
        count=len(rows), link="/report/rows/unknown_sku",
        details=_describe_pairs(db, rows),
    )


def _q_dispatch_errors(db: Session) -> list[DispatchQueueItem]:
    """Запрос отдельно от находки: те же строки нужны и полному списку."""
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.status == DispatchStatus.error,
        DispatchQueueItem.is_test.is_(False),
        # `or_` с проверкой на NULL обязателен: в SQL `NOT LIKE` по пустому полю
        # даёт NULL, то есть «не истина», и запись с незаполненной ошибкой
        # выпала бы из отчёта ВООБЩЕ — ни сюда, ни в находку про неизвестный sku.
        # Молча потерять ошибку рассылки хуже, чем показать её не в той группе.
        or_(DispatchQueueItem.last_error.is_(None),
            and_(*[~DispatchQueueItem.last_error.like(f"%{mark}%")
                   for mark in CARD_MISSING_MARKS])),
    ).all()
    # Только ПОСЛЕДНЯЯ запись пары товар+кабинет: всё, что было до неё,
    # описывает прошлое состояние, а не текущее.
    latest = latest_queue_ids(db)
    return [r for r in rows if r.id in latest]


def _check_dispatch_errors(db: Session) -> Finding | None:
    """Рассылка исчерпала попытки. Самое дорогое расхождение в системе.

    Позиции «площадка не знает такой sku» сюда НЕ входят: у них другое следствие
    и другой разбор, их показывает `_check_unknown_sku`. Смешать их значило бы
    написать про несуществующую карточку «она продолжает продавать то, чего
    нет» — и человек пошёл бы чинить связь вместо мэппинга.
    """
    rows = _q_dispatch_errors(db)
    if not rows:
        return None
    return Finding(
        key="dispatch_errors", level=CRITICAL,
        title=f"Рассылка не доехала до площадки: {len(rows)} записей",
        consequence="Остаток у нас уже списан, а на площадку новое число не ушло — "
                    "она продолжает продавать по старому, то есть продаёт то, чего нет.",
        count=len(rows), link="/report/rows/dispatch_errors",
        details=[f"{line} — {_error_gist(row.last_error)}"
                 for line, row in zip(_describe_pairs(db, rows), rows[:10])],
    )


def _sales_between(db: Session, row: DispatchQueueItem) -> int:
    """Сколько единиц этого товара площадка продала между отправкой и сверкой.

    Берём наши же принятые заказы по этой паре товар+кабинет. Окно начинается
    чуть раньше отправки: заказ, уменьшивший остаток на площадке, мог быть
    принят нами за считанные секунды до того, как ушло число, — и тогда он в
    отправленном значении уже учтён, а на площадке уже применён.

    Отменённые заказы не считаем: по ним площадка остаток вернула.
    """
    if row.sent_at is None:
        return 0
    until = row.verified_at or now_utc()
    total = db.query(func.coalesce(func.sum(ProcessedOrder.quantity), 0)).filter(
        ProcessedOrder.account_id == row.account_id,
        ProcessedOrder.uid_1c == row.uid_1c,
        ProcessedOrder.status != OrderProcessStatus.cancelled,
        ProcessedOrder.processed_at >= row.sent_at - timedelta(minutes=2),
        ProcessedOrder.processed_at <= until,
    ).scalar()
    return int(total or 0)


def _check_platform_divergence(db: Session) -> Finding | None:
    """Площадка держит не то, что мы ей отправили.

    Сверку делает отдельный воркер (`app/workers/verify_stock.py`) — он ходит в
    API площадок; отчёт читает уже сохранённое в очереди и наружу не ходит.

    Направление расхождения важнее самого факта, поэтому уровень от него и
    зависит. Площадка держит БОЛЬШЕ нашего — она продаёт то, чего нет, это
    оверселл и критично. МЕНЬШЕ — недоотправка: теряются продажи, но не деньги
    покупателя.

    19.09.2026 на боевом WB нашли ровно это: в кабинет писала вторая система
    (та, с которой идёт переход) и перетирала наши остатки за три минуты, а
    отправка каждый раз отвечала успехом. Без такой сверки это видно только
    глазами и только если пойти смотреть.
    """
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.verified_at.isnot(None),
        DispatchQueueItem.verified_quantity.isnot(None),
        DispatchQueueItem.sent_quantity.isnot(None),
        DispatchQueueItem.verified_quantity != DispatchQueueItem.sent_quantity,
        DispatchQueueItem.is_test.is_(False),
    ).order_by(DispatchQueueItem.verified_at.desc()).limit(200).all()
    # Площадка сама уменьшает остаток, когда товар покупают, — и между нашей
    # отправкой и сверкой проходит полчаса. 20.09 на бою обе «находки» были
    # ровно этим: отправили 39, площадка держит 38, отправили 29 — держит 28.
    # Называть продажу «наше число кто-то переписал» значит звать человека
    # разбирать штатную работу магазина, а отчёт, который зовёт зря, перестают
    # читать. Поэтому падение, объяснённое принятыми заказами, отбрасываем;
    # необъяснённое — оставляем, оно и есть расхождение.
    rows = [r for r in rows
            if r.verified_quantity > r.sent_quantity
            or (r.sent_quantity - r.verified_quantity) > _sales_between(db, r)]
    if not rows:
        return None

    higher = [r for r in rows if r.verified_quantity > r.sent_quantity]
    level = CRITICAL if higher else WARNING
    if higher:
        consequence = ("На площадке лежит БОЛЬШЕ, чем мы отправляли, — она продаёт "
                       "то, чего нет. Наше число кто-то переписал: либо в кабинет "
                       "пишет вторая система, либо отправка поняла ответ площадки "
                       "не так.")
    else:
        consequence = ("На площадке лежит МЕНЬШЕ, чем мы отправляли: продажи по этим "
                       "карточкам идут не с тем остатком, который есть на складе. "
                       "Наше число кто-то переписал поверх.")
    return Finding(
        key="platform_divergence", level=level,
        title=f"Площадка держит не то, что мы отправили: {len(rows)} позиций",
        consequence=consequence,
        count=len(rows), link="/diagnostics#accounts",
        details=[f"{r.sent_sku}: отправили {r.sent_quantity}, площадка держит "
                 f"{r.verified_quantity} ({_age(r.verified_at)} назад)" for r in rows[:10]],
    )


def _check_tasks_needing_review(db: Session) -> Finding | None:
    """Задания 1С, исчерпавшие автоповтор, — решение за человеком."""
    from app.workers.ftp_channel import tasks_needing_review   # локально: цикл импортов

    rows = tasks_needing_review(db)
    if not rows:
        return None
    return Finding(
        key="tasks_needing_review", level=CRITICAL,
        title=f"Заданий 1С ждут ручного разбора: {len(rows)}",
        consequence="Пока решение не принято, эти единицы считаются «в пути»: остаток "
                    "занижен, и наружу уходит меньше товара, чем есть на складе.",
        count=len(rows), link="/diagnostics#stuck-tasks",
        details=[f"заказ {t.order_id}, {t.barcode}, {t.quantity} шт" for t in rows[:10]],
    )


def _q_broadcast_without_recalc(db: Session) -> list[Product]:
    return db.query(Product).filter(
        Product.broadcast_enabled.is_(True),
        Product.recalc_done_at.is_(None),
    ).order_by(Product.article).all()


def _check_broadcast_without_recalc(db: Session) -> Finding | None:
    """Трансляция включена, а расчёта не было. Интерфейс такого не даёт
    (`products._blocks_broadcast_on`), поэтому строка здесь означает, что товар
    прошёл мимо интерфейса — импортом, правкой в базе или дефектом."""
    rows = _q_broadcast_without_recalc(db)
    if not rows:
        return None
    return Finding(
        key="broadcast_without_recalc", level=CRITICAL,
        title=f"Транслируются без расчёта: {len(rows)} товаров",
        consequence="На площадки уходит ПОЛНЫЙ остаток, не сверенный с их продажами. "
                    "Каждая такая карточка — прямой риск оверселла.",
        count=len(rows), link="/report/rows/broadcast_without_recalc",
        details=[f"{p.article or p.uid_1c} — {p.name or ''}"[:120] for p in rows[:10]],
    )


def _check_wb_without_chrt(db: Session) -> Finding | None:
    """Позиции WB, у которых в каталоге нет chrtId: остаток уходит баркодом.

    Спрятанная находка, которую нельзя увидеть ни на одной странице, пока её тут
    нет: отправка баркодом РАБОТАЕТ — сегодня. В спеке WB тело отправки остатков
    описано ключом `chrtId`, про `sku` там не сказано ни слова, зато заготовлен
    отказ `400 SKUUploadDisabled` («uploading stock is not allowed by 'sku'»).
    То есть приём баркода площадка умеет выключать, и в день, когда выключит,
    именно эти карточки перестанут получать остаток — молча, до первого отказа
    рассылки.

    Лечится не кодом, а выгрузкой каталога кабинета: chrtId приезжает оттуда
    (`external_id` = `nmID:chrtID`). Пустой правый кусок значит, что карточки
    размера в нашем снимке каталога нет — либо снимок старый, либо размер на
    площадке не заведён.

    Считаем ТЕМИ ЖЕ правилами, что и отправка (`wb._chrt_id`): правый кусок
    `external_id`, только цифры, не ноль. Разойдись счёт с отправкой — находка
    называла бы не то, что произойдёт на самом деле.
    """
    rows = _q_wb_without_chrt(db)
    if not rows:
        return None
    return Finding(
        key="wb_without_chrt", level=WARNING,
        title=f"На WB уходят баркодом, без chrtId: {len(rows)} пар",
        consequence="Сегодня работает, завтра может перестать: WB умеет отключать "
                    "приём остатков по баркоду (400 SKUUploadDisabled), и тогда эти "
                    "карточки перестанут получать остаток молча. Лечится выгрузкой "
                    "каталога кабинета — chrtId приезжает оттуда.",
        count=len(rows), link="/report/rows/wb_without_chrt",
        details=[f"{a} {size} {color} · {n} · {acc}".replace("  ", " ")[:120]
                 for a, size, color, n, acc in rows[:10]],
    )


def _q_wb_without_chrt(db: Session) -> list[tuple]:
    """(артикул, размер, цвет, наименование, кабинет) — пары без chrtId."""
    pairs = (
        db.query(Product.uid_1c, Product.article, Product.name,
                 Product.size, Product.color,
                 PlatformAccount.name.label("account"),
                 PlatformCatalogItem.external_id)
        .join(SyncSetting, SyncSetting.uid_1c == Product.uid_1c)
        .join(PlatformAccount, PlatformAccount.id == SyncSetting.account_id)
        .join(Barcode, Barcode.uid_1c == Product.uid_1c)
        .outerjoin(PlatformCatalogItem,
                   and_(PlatformCatalogItem.account_id == PlatformAccount.id,
                        PlatformCatalogItem.barcode == Barcode.barcode))
        .filter(SyncSetting.enabled.is_(True),
                Product.broadcast_enabled.is_(True),
                PlatformAccount.platform == Platform.wb)
        .all()
    )

    # Товар с несколькими баркодами даёт несколько строк, и chrtId может быть
    # хоть у одной. Достаточно одной: отправка возьмёт именно её.
    best: dict[tuple, tuple] = {}
    for uid, article, name, size, color, account, external_id in pairs:
        tail = (external_id or "").split(":")[-1].strip()
        has = tail.isdigit() and tail != "0"
        key = (uid, account)
        if key not in best or has:
            best[key] = (article or uid, size or "", color or "",
                         name or "", account, has)

    return sorted((v[:5] for v in best.values() if not v[5]))


def _check_breaker_disabled(db: Session) -> Finding | None:
    """Кабинет погашен предохранителем после пяти сбоев подряд."""
    rows = db.query(PlatformAccount).filter(
        PlatformAccount.is_active.is_(False),
        PlatformAccount.consecutive_failures > 0,
    ).all()
    if not rows:
        return None
    return Finding(
        key="breaker_disabled", level=CRITICAL,
        title=f"Кабинетов отключено предохранителем: {len(rows)}",
        consequence="Заказы по ним не опрашиваются вовсе: продажи идут, а у нас "
                    "не списывается ничего и в 1С не создаётся ни одного документа.",
        count=len(rows), link="/diagnostics#accounts",
        details=[f"{a.name}: {(a.last_error or '')[:100]}" for a in rows[:10]],
    )


def _check_stuck_1c_tasks(db: Session) -> Finding | None:
    """Задания, на которые 1С не ответила, — но ТОЛЬКО те, что ещё в работе.

    Задания, исчерпавшие автоповтор, показывает `_check_tasks_needing_review`
    отдельной критичной находкой. Без этого вычитания одна и та же строка
    попадала бы в отчёт дважды, а отчёт, который повторяется, читают
    невнимательно — ровно то, ради чего он написан, и потерялось бы.
    """
    from app.workers.ftp_channel import tasks_needing_review   # локально: цикл импортов

    review_ids = {t.id for t in tasks_needing_review(db)}
    cutoff = now_utc() - FTP_STUCK
    rows = [t for t in db.query(FtpTask).filter(
        FtpTask.status.in_([FtpTaskStatus.timeout, FtpTaskStatus.failed]),
        FtpTask.is_test.is_(False),
        FtpTask.created_at <= cutoff,
    ).all() if t.id not in review_ids]
    if not rows:
        return None
    units = sum(t.quantity or 0 for t in rows if t.command == "CREATE_MOVEMENT")
    return Finding(
        key="stuck_1c_tasks", level=WARNING,
        title=f"Заданий 1С без ответа дольше {_age(cutoff)}: {len(rows)}",
        consequence=f"Эти {units} ед. считаются «в пути» и вычитаются из остатка — "
                    "наружу уходит меньше, чем есть. Автоповтор ещё не исчерпан, "
                    "но если число не убывает, разбирать придётся руками.",
        count=len(rows), link="/diagnostics#stuck-tasks",
        details=[f"заказ {t.order_id}, {t.barcode}, {t.quantity} шт, {t.status.value}"
                 for t in rows[:10]],
    )


def _check_stale_catalog(db: Session) -> Finding | None:
    """Снимок каталога кабинета протух. Ровно случай 19.09: выгрузка не
    запускалась ни разу, а по каталогу берутся баркоды строк заказов Kit."""
    accounts = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all()
    cutoff = now_utc() - CATALOG_STALE
    stale = []
    for account in accounts:
        last = db.query(func.max(PlatformCatalogItem.fetched_at)).filter(
            PlatformCatalogItem.account_id == account.id,
        ).scalar()
        if last is not None and last > cutoff:
            continue
        if last is None and (account.created_at or now_utc()) > cutoff:
            # Кабинет завели только что — выгрузка ещё не успела отработать.
            # Ругать за это значит приучить оператора не читать отчёт.
            continue
        stale.append((account, last))
    if not stale:
        return None
    return Finding(
        key="stale_catalog", level=WARNING,
        title=f"Каталог протух у кабинетов: {len(stale)}",
        consequence="По каталогу узнаются баркоды строк заказа и идентификаторы для "
                    "отправки остатка. Устаревший каталог — это заказы по новым "
                    "карточкам, которые мы не разнесём, и остаток, который не уйдёт.",
        count=len(stale), link="/mapping",
        details=[f"{a.name}: последняя выгрузка {_age(last)} назад" for a, last in stale[:10]],
    )


def _scheduler_uptime(db: Session) -> timedelta | None:
    """Сколько работает планировщик. `None` — он ещё не стартовал ни разу.

    Нужно там, где отсутствие отметки само по себе ничего не значит: на свежей
    установке и сразу после рестарта «ни разу не отработало» — это норма, а не
    расхождение. Отличать одно от другого умеет только время работы процесса —
    ровно так же это делает `/health`.
    """
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "scheduler_start",
    ).first()
    return None if row is None else now_utc() - row.last_run_at


def _check_stale_reconciliation(db: Session) -> Finding | None:
    """Остаток ЦС давно не сверялся с 1С."""
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "reconciliation_applied",
    ).first()
    last = row.last_run_at if row else None
    if last is not None and last > now_utc() - RECONCILIATION_STALE:
        return None
    if last is None:
        # Ни одной сверки ещё не было. Это расхождение, только если планировщик
        # работает достаточно долго, чтобы она успела случиться; иначе мы ругаем
        # свежую установку за то, что она свежая.
        uptime = _scheduler_uptime(db)
        if uptime is None or uptime < RECONCILIATION_STALE:
            return None
    return Finding(
        key="stale_reconciliation", level=WARNING,
        title=f"Остаток ЦС не сверялся с 1С: {_age(last)}",
        consequence="Мы рассылаем на площадки своё представление об остатке, и чем "
                    "дольше оно не сверялось, тем дальше оно от того, что в 1С.",
        count=1, link="/diagnostics#workers",
    )


def _check_dispatch_stuck(db: Session) -> Finding | None:
    """Очередь рассылки стоит: записи ждут дольше, чем идёт цикл."""
    cutoff = now_utc() - DISPATCH_STUCK
    count = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.status == DispatchStatus.pending,
        DispatchQueueItem.is_test.is_(False),
        DispatchQueueItem.created_at <= cutoff,
    ).count()
    if not count:
        return None
    return Finding(
        key="dispatch_stuck", level=WARNING,
        title=f"Записей рассылки ждут дольше {_age(cutoff)}: {count}",
        consequence="Остаток у нас уже изменился, а на площадке всё ещё старое число. "
                    "Либо площадка не отвечает, либо цикл рассылки не идёт.",
        count=count, link="/diagnostics#accounts",
    )


def _check_open_anomalies(db: Session) -> Finding | None:
    """Неразобранные аномалии — но НЕ ВСЕ, а только те, что действительно наши.

    `order_on_disabled` («заказ по неподключенному товару») в отчёт не попадает
    СОЗНАТЕЛЬНО. Пока идёт переход, остатки по части каталога транслирует ещё
    старая система, и продажи по этим карточкам идут мимо нас в порядке вещей:
    товар у нас не отмечен, остаток мы по нему не рассылали и не списывали,
    разбирать тут нечего. Следствия — того самого, которое обязана называть
    каждая находка, — здесь просто нет, а значит это не расхождение, а мера
    того, какая часть каталога ещё не переехала. С полным переходом эти строки
    исчезнут сами; до тех пор они бы держали отчёт постоянно непустым, и человек
    привык бы его пролистывать. Ровно так инструмент и умирает.

    `missing_barcode` — другое дело. Там заказ ЕСТЬ, а разнести его не на что:
    перемещение в 1С не создано и не создастся, пока баркод не сопоставят, и
    каждый следующий заказ по нему повторит то же самое. Это не рассосётся
    переходом.
    """
    rows = db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new,
        SyncAnomaly.is_test.is_(False),
        SyncAnomaly.reason == AnomalyReason.missing_barcode,
    ).all()
    if not rows:
        return None
    oldest = min(a.detected_at for a in rows if a.detected_at is not None) \
        if any(a.detected_at for a in rows) else None
    level = WARNING if oldest is None or oldest > now_utc() - ANOMALY_OLD else CRITICAL
    return Finding(
        key="open_anomalies", level=level,
        title=f"Заказов без сопоставленного баркода: {len(rows)} (старейшему {_age(oldest)})",
        consequence="Товар продан, а разнести продажу не на что: документа в 1С нет и "
                    "не будет, пока баркод не сопоставят. Каждый следующий заказ по "
                    "этому баркоду повторит то же самое.",
        count=len(rows), link="/anomalies",
    )


def _check_open_stock_date(db: Session) -> Finding | None:
    """Заявка на срез остатков на дату, на которую 1С не ответила."""
    cutoff = now_utc() - STOCK_DATE_STUCK
    rows = db.query(StockDateSnapshot).filter(
        StockDateSnapshot.status.in_([StockDateStatus.pending, StockDateStatus.sent]),
        StockDateSnapshot.created_at <= cutoff,
    ).all()
    if not rows:
        return None
    return Finding(
        key="open_stock_date", level=WARNING,
        title=f"Заявок на срез без ответа 1С: {len(rows)}",
        consequence="Товары с этой базовой датой стоят в «ждём выгрузку 1С»: порог "
                    "по ним не посчитан, расчёт не закрыт, трансляцию включить нельзя.",
        count=len(rows), link="/stock-on-date",
        details=[f"на {s.snapshot_date}, заказан {_age(s.created_at)} назад" for s in rows[:10]],
    )


def _check_mapping_conflicts(db: Session) -> Finding | None:
    """Баркоды с площадок, которых нет в 1С."""
    count = db.query(MappingConflict).count()
    if not count:
        return None
    return Finding(
        key="mapping_conflicts", level=WARNING,
        title=f"Несопоставленных баркодов: {count}",
        consequence="Заказ по такому баркоду разнести не на что: остаток не спишется, "
                    "документ в 1С не создастся, заказ уйдёт в аномалии.",
        count=count, link="/mapping",
    )


def _q_negative_stock(db: Session) -> list[Product]:
    return db.query(Product).filter(
        Product.stock_on_hand < 0).order_by(Product.article).all()


def _check_negative_stock(db: Session) -> Finding | None:
    """Отрицательный остаток — пересортица, не ошибка кода."""
    rows = _q_negative_stock(db)
    if not rows:
        return None
    return Finding(
        key="negative_stock", level=WARNING,
        title=f"Товаров с отрицательным остатком: {len(rows)}",
        consequence="Мы списали больше, чем числилось. Наружу по ним уходит ноль, "
                    "то есть продажи по этим карточкам стоят до разбора в 1С.",
        count=len(rows), link="/report/rows/negative_stock",
        details=[f"{p.article or p.uid_1c}: {p.stock_on_hand}" for p in rows[:10]],
    )


def _check_reconciliation_review(db: Session) -> Finding | None:
    """Крупные расхождения со складом 1С за последние сутки.

    Раньше находка считала строки `needs_review`, которые ждут ручного решения.
    Ждать их больше некому: с сентябрьской правки сверка применяет ЛЮБОЕ
    движение склада сама (см. `reconciliation.run_reconciliation`) и тут же
    ставит записи `resolved=True`. Новых неразрешённых не появляется вовсе, а
    старые, от прежней версии, не рассосутся никогда — 20.09 на бою их было 909
    штук от 14–16.09, и они держали отчёт жёлтым круглосуточно.

    Но сам сигнал терять нельзя: крупная дельта — это пересортица или ошибка
    учёта, и её стоит видеть. Поэтому находка теперь про СВЕЖИЕ крупные
    расхождения, независимо от того, применены они или нет: остаток по ним уже
    переписан по 1С, а вот почему он разошёлся — вопрос к складу.
    """
    since = now_utc() - RECONCILIATION_WINDOW
    count = db.query(ReconciliationLog).filter(
        ReconciliationLog.classification == ReconciliationClassification.needs_review,
        ReconciliationLog.checked_at >= since,
    ).count()
    if not count:
        return None
    rows = db.query(ReconciliationLog).filter(
        ReconciliationLog.classification == ReconciliationClassification.needs_review,
        ReconciliationLog.checked_at >= since,
    ).order_by(ReconciliationLog.checked_at.desc()).limit(10).all()
    products = {p.uid_1c: p for p in db.query(Product).filter(
        Product.uid_1c.in_({r.uid_1c for r in rows})).all()} if rows else {}

    def line(row):
        product = products.get(row.uid_1c)
        article = (product.article if product else "") or row.uid_1c
        name = ((product.name if product else "") or "")[:40]
        # Именно «у нас было / в 1С стало»: разница сама по себе ни о чём не
        # говорит, а пара чисел сразу показывает, куда уехал склад.
        return (f"{article} · {name} — у нас было {row.python_stock}, "
                f"в 1С {row.actual_1c} (разница {row.delta:+d})")

    return Finding(
        key="reconciliation_review", level=WARNING,
        title=f"Крупных расхождений со складом 1С за сутки: {count}",
        consequence="Наш остаток разошёлся с 1С сильнее порога. Сверка уже "
                    "переписала его по 1С — то есть наружу уходит число из 1С, — "
                    "но сама разница означает пересортицу или ошибку учёта на "
                    "складе, и её стоит разобрать там.",
        count=count, link="/diagnostics#reconciliation",
        details=[line(r) for r in rows],
    )


def _check_worker_failures(db: Session) -> Finding | None:
    """Воркеры, последний прогон которых закончился ошибкой."""
    rows = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.last_success.is_(False),
    ).all()
    if not rows:
        return None
    return Finding(
        key="worker_failures", level=WARNING,
        title=f"Воркеров с ошибкой в последнем прогоне: {len(rows)}",
        consequence="Задание отработало неуспешно. Что именно встало — видно по "
                    "имени: опрос заказов, рассылка, канал 1С или сверка.",
        count=len(rows), link="/diagnostics#workers",
        details=[f"{r.worker_name}: {(r.last_error or '')[:100]}" for r in rows[:10]],
    )


CHECKS = (
    _check_dispatch_errors,
    _check_unknown_sku,
    _check_platform_divergence,
    _check_tasks_needing_review,
    _check_broadcast_without_recalc,
    _check_wb_without_chrt,
    _check_breaker_disabled,
    _check_stuck_1c_tasks,
    _check_stale_catalog,
    _check_stale_reconciliation,
    _check_dispatch_stuck,
    _check_open_anomalies,
    _check_open_stock_date,
    _check_mapping_conflicts,
    _check_negative_stock,
    _check_reconciliation_review,
    _check_worker_failures,
)


def collect_findings(db: Session) -> list[Finding]:
    """Все расхождения, критичные первыми. Пустой список — всё в порядке.

    Одна упавшая проверка не должна уносить весь отчёт: расхождения независимы,
    и «ничего не показали, потому что где-то ошибка» — худший из исходов для
    инструмента, смысл которого в том, чтобы показывать.
    """
    findings: list[Finding] = []
    for check in CHECKS:
        try:
            found = check(db)
        except Exception as e:                      # noqa: BLE001 — см. docstring
            findings.append(Finding(
                key=f"{check.__name__}_failed", level=WARNING,
                title=f"Проверка {check.__name__} не отработала",
                consequence="Это расхождение сейчас не контролируется — отчёт по нему "
                            "ничего не знает, а не подтверждает, что всё хорошо.",
                details=[f"{type(e).__name__}: {e}"[:200]],
            ))
            continue
        if found is not None:
            findings.append(found)
    findings.sort(key=lambda f: (LEVEL_ORDER.get(f.level, 9), f.key))
    return findings


def summary_line(findings: list[Finding]) -> str:
    """Одна строка для лога. Читается без открывания страницы — в этом смысл."""
    if not findings:
        return "расхождений нет"
    critical = [f for f in findings if f.level == CRITICAL]
    parts = [f"{f.key}={f.count}" for f in findings]
    head = f"критичных {len(critical)} из {len(findings)}"
    return f"{head}: " + ", ".join(parts)


# --------------------------------------------------------- полные списки
# Находка показывает десять строк и пишет «и ещё N — полный список по ссылке
# ниже». 21.09 выяснилось, что ссылка ведёт в общий каталог товаров, где
# никакого списка нет: обещание было, страницы не было. Здесь она и живёт.
#
# Списки собираются ОТДЕЛЬНО от находок и только по запросу страницы. Держать
# их внутри `Finding` нельзя: отчёт собирается раз в час и на каждой загрузке
# «Диагностики», а строк бывают тысячи.


def _rows_dispatch_errors(db: Session) -> list[list[str]]:
    return _queue_rows(db, _q_dispatch_errors(db))


def _rows_unknown_sku(db: Session) -> list[list[str]]:
    return _queue_rows(db, _q_unknown_sku(db))


def _queue_rows(db: Session, rows: list[DispatchQueueItem]) -> list[list[str]]:
    """Строки очереди в виде, с которым идут разбираться: товар, кабинет, причина."""
    uids = {r.uid_1c for r in rows}
    products = {p.uid_1c: p for p in
                db.query(Product).filter(Product.uid_1c.in_(uids)).all()} if uids else {}
    names = {a.id: a.name for a in db.query(PlatformAccount).all()}
    out = []
    for r in rows:
        p = products.get(r.uid_1c)
        out.append([
            (p.article if p else "") or r.uid_1c,
            (p.size if p else "") or "",
            (p.color if p else "") or "",
            (p.name if p else "") or "",
            names.get(r.account_id, ""),
            r.sent_sku or "",
            str(r.quantity),
            _error_gist(r.last_error, limit=400),
        ])
    return out


def _rows_wb_without_chrt(db: Session) -> list[list[str]]:
    return [[a, size, color, n, acc, "баркодом"]
            for a, size, color, n, acc in _q_wb_without_chrt(db)]


def _rows_broadcast_without_recalc(db: Session) -> list[list[str]]:
    return [[p.article or p.uid_1c, p.size or "", p.color or "", p.name or "",
             str(p.stock_on_hand or 0)]
            for p in _q_broadcast_without_recalc(db)]


def _rows_negative_stock(db: Session) -> list[list[str]]:
    return [[p.article or p.uid_1c, p.size or "", p.color or "", p.name or "",
             str(p.stock_on_hand or 0)]
            for p in _q_negative_stock(db)]


QUEUE_COLUMNS = ["Артикул", "Размер", "Цвет", "Наименование", "Кабинет",
                 "SKU, которым ушло", "Количество", "Что ответила площадка"]
PRODUCT_COLUMNS = ["Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС"]

# key находки → (заголовок страницы, колонки, функция строк).
FULL_LISTS = {
    "dispatch_errors": ("Рассылка не доехала до площадки", QUEUE_COLUMNS,
                        _rows_dispatch_errors),
    "unknown_sku": ("Площадка не знает наш sku", QUEUE_COLUMNS, _rows_unknown_sku),
    "wb_without_chrt": ("На WB уходят баркодом, без chrtId",
                        ["Артикул", "Размер", "Цвет", "Наименование", "Кабинет",
                         "Чем адресуется"], _rows_wb_without_chrt),
    "broadcast_without_recalc": ("Транслируются без расчёта", PRODUCT_COLUMNS,
                                 _rows_broadcast_without_recalc),
    "negative_stock": ("Товары с отрицательным остатком", PRODUCT_COLUMNS,
                       _rows_negative_stock),
}
