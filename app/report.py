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
# Копия базы снимается раз в сутки. Двое суток — одна пропущенная копия плюс
# запас на перезапуск воркера; дальше это уже не «не сложилось».
BACKUP_STALE = timedelta(days=2)


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
# Тексты остаются ТОЛЬКО ради записей, лежащих в базе с прежних времён: признак
# теперь несёт колонка `DispatchQueueItem.card_missing`, которую ставит рассылка
# по ответу площадки. Подстроками это не работало: у Kit слова другие
# («площадка не знает такой ТОВАР (variant_id …)», «карточка товара в архиве»),
# а Ozon кладёт в `detail` сообщение площадки по-английски — ни одна русская
# метка туда не попадала. Все такие записи становились КРИТИЧНОЙ находкой
# «рассылка не доехала: остаток списан, площадка продаёт то, чего нет» — при
# том, что продавать там нечего вовсе. И навсегда: запись терминальная,
# успешной отправки по паре не будет, снять её нечем ни `latest_queue_ids`, ни
# `_not_overtaken_by_a_later_send`, ни `_only_live_pairs`.
CARD_MISSING_MARKS = (UNKNOWN_SKU_MARK, NO_CARD_MARK)


def _card_missing_clause():
    """«Карточки нет» — по колонке, а для старых записей ещё и по тексту."""
    return or_(DispatchQueueItem.card_missing.is_(True),
               *[DispatchQueueItem.last_error.like(f"%{mark}%")
                 for mark in CARD_MISSING_MARKS])


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
        _card_missing_clause(),
    ).all()
    # Пара товар+кабинет считается один раз: после двух прогонов массовой
    # переотправки по одному и тому же нерешённому товару лежит две записи, а
    # разбирать человеку нечего дважды — это одна неразрешённая пара.
    latest = latest_queue_ids(db)
    rows = [r for r in rows if r.id in latest]
    # Карточку могли завести, и тогда число уехало — а отказ остался лежать и
    # через тридцать суток снова станет последней записью пары, когда чистка
    # удалит перекрывшую его `sent`. Та же дыра, что и у «рассылка не доехала».
    rows = _not_overtaken_by_a_later_send(db, rows)
    # И только по живым парам: по выключенной решать нечего — решение уже
    # принято, товар туда не транслируется.
    return _only_live_pairs(db, rows)


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
        # Ровно дополнение к `_card_missing_clause`. `or_` с проверкой на NULL
        # обязателен: в SQL `NOT LIKE` по пустому полю даёт NULL, то есть «не
        # истина», и запись с незаполненной ошибкой выпала бы из отчёта ВООБЩЕ —
        # ни сюда, ни в находку про неизвестный sku. Молча потерять ошибку
        # рассылки хуже, чем показать её не в той группе.
        DispatchQueueItem.card_missing.is_(False),
        or_(DispatchQueueItem.last_error.is_(None),
            and_(*[~DispatchQueueItem.last_error.like(f"%{mark}%")
                   for mark in CARD_MISSING_MARKS])),
    ).all()
    # Только ПОСЛЕДНЯЯ запись пары товар+кабинет: всё, что было до неё,
    # описывает прошлое состояние, а не текущее.
    latest = latest_queue_ids(db)
    rows = [r for r in rows if r.id in latest]
    return _only_live_pairs(db, _not_overtaken_by_a_later_send(db, rows))


def _not_overtaken_by_a_later_send(db: Session,
                                  rows: list[DispatchQueueItem]) -> list[DispatchQueueItem]:
    """Отбросить отказы, по паре которых число уехало ПОЗЖЕ.

    Это уже делает `latest_queue_ids` — но ровно до тех пор, пока перекрывающая
    запись `sent` жива. Чистка удаляет её через тридцать суток, а отказ не
    удаляет никогда (намеренно: `error` описывает незаконченное дело). И тогда
    последней записью пары СНОВА становится старый отказ, и он воскресает
    КРИТИЧНОЙ находкой «площадка продаёт то, чего нет» — про число, доехавшее
    месяц назад. Разобрать такую находку нельзя ничем: статуса запись не сменит,
    удалена не будет, и отчёт остаётся красным навсегда.

    Это не гипотеза и не «когда-нибудь»: на бою лежат сотни отказов от дефекта
    Kit 14–20.09, по которым остаток потом уехал. Их прикрытие начнёт исчезать
    ровно через тридцать суток после той отправки.

    Спрашиваем `SyncSetting.last_nonzero_sent_at` — она для того и заведена, что
    переживает чистку: очередь это недолговечная память, а пара помнит, что мы
    на неё писали. Сравнение строго ПОЗЖЕ создания отказа: отправка, бывшая
    раньше него, про него ничего не говорит, и заглушить его ею значило бы
    потерять настоящее расхождение.
    """
    if not rows:
        return rows
    sent_at = {
        (s.uid_1c, s.account_id): s.last_nonzero_sent_at for s in
        db.query(SyncSetting.uid_1c, SyncSetting.account_id,
                 SyncSetting.last_nonzero_sent_at).filter(
            SyncSetting.uid_1c.in_({r.uid_1c for r in rows}),
            SyncSetting.last_nonzero_sent_at.isnot(None)).all()
    }
    out = []
    for r in rows:
        later = sent_at.get((r.uid_1c, r.account_id))
        if later is not None and r.created_at is not None and later > r.created_at:
            continue
        out.append(r)
    return out


def _only_live_pairs(db: Session, rows: list[DispatchQueueItem]) -> list[DispatchQueueItem]:
    """Отбросить отказы, которые уже ничего не означают.

    21.09 на бою отчёт держал КРИТИЧНУЮ находку по записи очереди **id=1** —
    самой первой в системе, созданной 14.09, от дефекта Kit, починенного
    двадцатого. Кабинет по этой паре не отмечен, и непустой остаток мы туда не
    отправляли ни разу.

    Тогда следствие находки — «остаток списан, а площадка продолжает продавать
    по старому числу» — просто НЕПРАВДА: площадка держит наше число, только
    если мы его туда посылали. А сама запись мёртвая: рассылка её не возьмёт
    (галочки нет), статуса она не сменит никогда, и отчёт остался бы красным
    навсегда. Вечно красный отчёт пролистывают не читая — это уже проходили с
    650 записями от того же дефекта.

    Условия ОБА, и второе обязательно. Снятая галочка сама по себе отказ не
    отменяет: отправляли 50, человек снял галочку, отзыв (ноль) не доехал — на
    площадке по-прежнему лежит 50, и она продаёт то, чего нет. Это настоящее
    расхождение, и `ever_transmitted` его сохраняет.

    Отсутствие товара в номенклатуре поводом НЕ считается: запись очереди
    переживает удаление товара, и строка обязана появиться всё равно — иначе
    находка насчитает больше, чем покажет (см. одноимённый тест).

    Ничего не удаляем: пару включат — отказ вернётся в отчёт сам.
    """
    if not rows:
        return rows
    ticked = {
        (s.uid_1c, s.account_id) for s in
        db.query(SyncSetting.uid_1c, SyncSetting.account_id).filter(
            SyncSetting.uid_1c.in_({r.uid_1c for r in rows}),
            SyncSetting.enabled.is_(True)).all()
    }

    from app.transmit import ever_transmitted

    return [r for r in rows
            if (r.uid_1c, r.account_id) in ticked
            or ever_transmitted(db, r.uid_1c, r.account_id)]


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


def _only_latest_send(db: Session,
                     rows: list[DispatchQueueItem]) -> list[DispatchQueueItem]:
    """Отбросить строки, по паре которых ПОЗЖЕ ушло новое число.

    Сверка перепроверяет только ПОСЛЕДНЮЮ отправку по товару
    (`verify_stock.rows_to_verify`), а находка брала любую строку с
    несовпадением. Разница и есть дефект: как только по товару уходит новое
    число, старая строка застывает со своим расхождением навсегда — сверка её
    больше не тронет, статуса она не сменит, и отчёт остаётся красным вечно.

    Найдено на бою 22.09 в чистом виде. Находка показывала три позиции
    («отправили 60, площадка держит 59»), а сверка тем же часом отвечала
    `diverged: 2` — и это были РАЗНЫЕ строки: по всем трём из находки уже
    прошла новая отправка (59, 54, 0), которую площадка и держала. Обычная
    продажа: WB списал единицу, мы приняли заказ и отправили новое число.
    Разобрать такую находку нельзя ничем — она описывает прошлое.

    Тот же приём, что у `_check_dispatch_errors` с отказами, по паре которых
    позже была успешная отправка: вечно красный отчёт оператор пролистывает не
    читая, и тогда он бесполезен весь.
    """
    if not rows:
        return rows
    uids = {r.uid_1c for r in rows}
    accounts = {r.account_id for r in rows}
    latest: dict[tuple[str, int], datetime] = {}
    for uid, account_id, when in db.query(
            DispatchQueueItem.uid_1c, DispatchQueueItem.account_id,
            func.max(DispatchQueueItem.sent_at),
    ).filter(
        DispatchQueueItem.uid_1c.in_(uids),
        DispatchQueueItem.account_id.in_(accounts),
        DispatchQueueItem.sent_at.isnot(None),
        DispatchQueueItem.is_test.is_(False),
    ).group_by(DispatchQueueItem.uid_1c, DispatchQueueItem.account_id).all():
        latest[(uid, account_id)] = when
    return [r for r in rows
            if r.sent_at is not None
            and latest.get((r.uid_1c, r.account_id)) == r.sent_at]


# Сколько строк расхождения поднимаем за раз. Предел нужен — необъяснённость
# каждой строки проверяется отдельным запросом (`_sales_between`), — но он
# стоит ПОСЛЕ отсева, а не до. Раньше `.limit(200)` шёл до `_only_latest_send`
# и до отбрасывания продаж, поэтому `count` не мог превысить двухсот НИ ПРИ
# КАКОМ числе расхождений, а само обрезание ничем не помечалось: отчёт молча
# показывал двести и выглядел точным. Речь о единственной находке, которая
# ловит перезапись наших остатков второй системой.
DIVERGENCE_SCAN_LIMIT = 2000


def _q_platform_divergence(db: Session) -> list[DispatchQueueItem]:
    """Запрос отдельно от находки: те же строки нужны и полному списку.

    Разойдись они — список показывал бы не то, что насчитала находка.
    """
    rows = db.query(DispatchQueueItem).filter(
        DispatchQueueItem.verified_at.isnot(None),
        DispatchQueueItem.verified_quantity.isnot(None),
        DispatchQueueItem.sent_quantity.isnot(None),
        DispatchQueueItem.verified_quantity != DispatchQueueItem.sent_quantity,
        DispatchQueueItem.is_test.is_(False),
    ).order_by(DispatchQueueItem.verified_at.desc()).limit(DIVERGENCE_SCAN_LIMIT).all()
    rows = _only_latest_send(db, rows)
    # Площадка сама уменьшает остаток, когда товар покупают, — и между нашей
    # отправкой и сверкой проходит полчаса. 20.09 на бою обе «находки» были
    # ровно этим: отправили 39, площадка держит 38, отправили 29 — держит 28.
    # Называть продажу «наше число кто-то переписал» значит звать человека
    # разбирать штатную работу магазина, а отчёт, который зовёт зря, перестают
    # читать. Поэтому падение, объяснённое принятыми заказами, отбрасываем;
    # необъяснённое — оставляем, оно и есть расхождение.
    return [r for r in rows
            if r.verified_quantity > r.sent_quantity
            or (r.sent_quantity - r.verified_quantity) > _sales_between(db, r)]


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
    rows = _q_platform_divergence(db)
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
        count=len(rows), link="/report/rows/platform_divergence",
        details=[f"{r.sent_sku}: отправили {r.sent_quantity}, площадка держит "
                 f"{r.verified_quantity} ({_age(r.verified_at)} назад)" for r in rows[:10]],
    )


def _check_tasks_needing_review(db: Session) -> Finding | None:
    """Задания 1С, исчерпавшие автоповтор, — решение за человеком.

    Следствие зависит от КОМАНДЫ, и одним текстом на всех его описывать нельзя.
    Открытое `CREATE_MOVEMENT` считается «в пути» со знаком плюс: остаток
    занижен, наружу уходит меньше, чем есть. Открытое `CANCEL_MOVEMENT` — со
    знаком минус: остаток ЗАВЫШЕН, наружу уходит БОЛЬШЕ, то есть прямой
    оверселл, и это противоположный случай. В разбор попадает любая команда,
    кроме `CREATE_MOVEMENT` (автоповтор берёт только его), так что отмен здесь
    не «иногда», а по построению.

    Раньше находка давала одно следствие на всех — по созданию, — а `details`
    команду не называли вовсе, так что отличить было нельзя. Правильный текст
    при этом уже лежал рядом, в `_stuck_rows` на «Диагностике»: расходились три
    текста про одну сущность, и человек, читающий их буквально, перестаёт верить
    всем трём.
    """
    from app.workers.ftp_channel import tasks_needing_review   # локально: цикл импортов

    rows = tasks_needing_review(db)
    if not rows:
        return None
    cancels = [t for t in rows if t.command == "CANCEL_MOVEMENT"]
    consequence = ("Пока решение не принято, эти единицы считаются «в пути»: остаток "
                   "занижен, и наружу уходит меньше товара, чем есть на складе.")
    if cancels:
        consequence += (f" Из них отмен: {len(cancels)} — у них знак ОБРАТНЫЙ: "
                        f"остаток завышен, наружу уходит больше, чем есть, "
                        f"то есть прямой оверселл.")
    return Finding(
        key="tasks_needing_review", level=CRITICAL,
        title=f"Заданий 1С ждут ручного разбора: {len(rows)}",
        consequence=consequence,
        count=len(rows), link="/diagnostics#stuck-tasks",
        details=[f"{t.command}, заказ {t.order_id}, {t.barcode}, {t.quantity} шт"
                 for t in rows[:10]],
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


# Сколько просьба «включить трансляцию» может честно ждать своего расчёта.
# Расчёт идёт минутами, ответ 1С на дату — до десяти минут; сутки означают, что
# ждать уже нечего.
PENDING_BROADCAST_STUCK = timedelta(hours=24)


def _q_stuck_broadcast_requests(db: Session) -> list[Product]:
    return db.query(Product).filter(
        Product.broadcast_requested_at.isnot(None),
        Product.broadcast_enabled.is_(False),
        Product.broadcast_requested_at <= now_utc() - PENDING_BROADCAST_STUCK,
    ).order_by(Product.broadcast_requested_at).all()


def _check_stuck_broadcast_requests(db: Session) -> Finding | None:
    """Файл попросил включить трансляцию, а она не включилась за сутки.

    Просьба (`Product.broadcast_requested_at`) — это обещание: оператор одним
    файлом задал дату, факт, кабинеты и «Трансляция = Да», а включит строку тот,
    кто имеет право, — расчёт или приём ответа 1С, ровно тогда, когда включила бы
    и страница. Обещание сдержано в подавляющем большинстве случаев, и потому
    несдержанное особенно незаметно: оператор считает, что сделал работу файлом,
    и больше к этим строкам не возвращается, а товар всё это время молчит —
    остаток наружу не уходит, продаж нет, и никакой ошибки нигде не горит.

    Зависнуть просьба может на том, что само не рассосётся: расчёт кончился
    проблемами (неполная лента заказов, потерянные строки Kit), кабинет погасил
    предохранитель, факт так и не ввели. Самый частый случай — последний — теперь
    отсекается на входе (`products_import`: просьба из «ждём 1С» без факта
    становится ошибкой импорта сразу). Эта находка ловит остальные.
    """
    rows = _q_stuck_broadcast_requests(db)
    if not rows:
        return None
    oldest = min(p.broadcast_requested_at for p in rows)
    return Finding(
        key="stuck_broadcast_requests", level=WARNING,
        title=f"Просьб включить трансляцию не выполнено: {len(rows)} "
              f"(старейшей {_age(oldest)})",
        consequence="Оператор включил эти строки файлом и считает работу сделанной, "
                    "а трансляция так и не включилась: остаток наружу не уходит, "
                    "продаж по ним нет. Строки молчат, и сами они не включатся.",
        count=len(rows), link="/report/rows/stuck_broadcast_requests",
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


def _check_suspicious_snapshot(db: Session) -> Finding | None:
    """Сверка перестала обнулять распроданное — выгрузка 1С пришла обрезанной.

    Когда снимок покрывает меньше половины прежних ненулевых товаров, сверка
    включает предохранитель и ничего не обнуляет. Предохранитель правильный:
    обрезанный файл не должен стереть весь каталог. Но следствие у его
    срабатывания тяжёлое и отложенное — распроданный товар продолжает
    транслироваться, то есть на площадки уходит остаток по тому, чего на складе
    нет. И состояние самоподдерживающееся: снимок сам не вырастет, час за часом
    будет одно и то же.

    До аудита 21.09 об этом не говорил НИКТО: признак попадал в `stats` и уходил
    одной строкой в журнал.
    """
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "reconciliation_applied",
    ).first()
    if row is None or not row.last_error:
        return None
    if "обнуление" not in row.last_error:
        return None
    return Finding(
        key="suspicious_snapshot", level=CRITICAL,
        title=f"Выгрузка 1С пришла неполной — {row.last_error}",
        consequence="Распроданные товары не обнуляются: на площадки продолжает "
                    "уходить остаток по тому, чего на складе нет. Само не пройдёт — "
                    "пока выгрузка приходит обрезанной, так будет каждый час.",
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

    ОГОВОРКА, найденная аудитом 21.09. Аномалию этого типа сейчас не создаёт ни
    одна строка кода: заказ с неизвестным баркодом `process_new_order` отдаёт со
    статусом `unmatched`, а `resolve_barcode` пишет `MappingConflict` — аномалию
    завести и нельзя, у неё `uid_1c` обязателен, а его-то как раз и нет. Значит
    эта проверка сегодня молчит всегда, а живой сигнал по тому же случаю даёт
    `_check_mapping_conflicts` ниже. Проверку не удаляем: аномалия может
    появиться (например, баркод сопоставлен, а товар удалён), и тогда она нужна
    именно здесь — но полагаться на неё как на ЕДИНСТВЕННЫЙ сигнал нельзя.
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
    """Баркоды, по которым ПРИШЁЛ ЗАКАЗ, а разнести его не на что.

    Строка `MappingConflict` заводится из двух мест, и следствия у них разные.
    `resolve_barcode` (приём заказа) — это уже случившаяся непроведённая
    продажа, и `attempts` считает, сколько раз она повторилась. `catalog_sync`
    (суточная выгрузка каталога) — это карточка площадки, которой нет в 1С:
    заказов по ней ноль, товара в 1С нет, завышать нечего, и делать с ней надо
    ровно то же, что со страницей «Есть на складе — нет на площадке», только с
    другой стороны.

    До аудита 22.09 находка брала ВСЕ строки без разбора и печатала их число
    как «заказов по ним N», обещая «остаток товара завышен ровно на эти
    продажи». Про каталожные строки это была неправда, а уровень через трое
    суток становился КРИТИЧНЫМ и не гас: чистит их только появление баркода в
    1С, а суточная выгрузка заводит новые.

    Различаем по счётчику заказов: `catalog_sync` ставит 0, `resolve_barcode`
    увеличивает на каждом заказе. Строка, заведённая каталогом, переедет сюда
    сама, как только по ней придёт первый заказ.

    Уровень зависит от возраста, как у аномалий: свежий конфликт — это
    «сопоставьте на днях», а висящий сутками означает, что продажи по нему идут
    мимо нас всё это время.
    """
    rows = [c for c in db.query(MappingConflict).all() if (c.attempts or 0) > 0]
    if not rows:
        return None
    oldest = min((c.first_seen for c in rows if c.first_seen is not None), default=None)
    level = WARNING if oldest is None or oldest > now_utc() - ANOMALY_OLD else CRITICAL
    attempts = sum(c.attempts or 0 for c in rows)
    return Finding(
        key="mapping_conflicts", level=level,
        title=f"Несопоставленных баркодов с заказами: {len(rows)} "
              f"(заказов по ним {attempts}, старейшему {_age(oldest)})",
        consequence="По этим баркодам уже приходили заказы, и разнести их не на что: "
                    "остаток не списан, документа в 1С нет. Остаток товара завышен "
                    "ровно на эти продажи, и на площадки уходит больше, чем есть.",
        count=len(rows), link="/mapping",
    )


def _check_catalog_cards_without_1c(db: Session) -> Finding | None:
    """Карточка на площадке есть, а баркода в 1С нет — заказов по ней не было.

    Ровно те строки `MappingConflict`, что завела суточная выгрузка каталога.
    Продаж по ним не случилось ни одной, остаток ничем не завышен, оверселла
    нет — поэтому и уровень свой, и текст свой. Вместе с заказными они давали
    КРИТИЧНУЮ находку про несписанные продажи, которых не было.
    """
    rows = [c for c in db.query(MappingConflict).all() if (c.attempts or 0) == 0]
    if not rows:
        return None
    oldest = min((c.first_seen for c in rows if c.first_seen is not None), default=None)
    return Finding(
        key="catalog_cards_without_1c", level=WARNING,
        title=f"Карточек площадок без баркода в 1С: {len(rows)} "
              f"(старейшей {_age(oldest)})",
        consequence="Заказов по ним ещё не было, остаток ничем не завышен. Но как "
                    "только заказ придёт, разнести его будет не на что — и это "
                    "уже будет несписанная продажа. Разбирается сопоставлением.",
        count=len(rows), link="/mapping",
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


def _q_reconciliation_review(db: Session) -> list[ReconciliationLog]:
    """Крупные расхождения ЗА ПОСЛЕДНИЕ СУТКИ.

    Окно здесь и срок кнопки «Закрыть старые расхождения» на «Диагностике»
    намеренно не пересекаются: кнопка берёт то, что СТАРШЕ суток, находка — то,
    что моложе. Поэтому кнопка эту находку не гасит и гасить не должна, а
    оператор, нажавший её и не увидевший изменений, прав в своём недоумении —
    об этом теперь сказано прямо в тексте находки.
    """
    return db.query(ReconciliationLog).filter(
        ReconciliationLog.classification == ReconciliationClassification.needs_review,
        ReconciliationLog.checked_at >= now_utc() - RECONCILIATION_WINDOW,
    ).order_by(ReconciliationLog.checked_at.desc()).all()


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
    all_rows = _q_reconciliation_review(db)
    count = len(all_rows)
    if not count:
        return None
    rows = all_rows[:10]
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
                    "складе, и разбирать её надо В 1С. Кнопка «Закрыть старые "
                    "расхождения» на «Диагностике» этих строк НЕ касается: она "
                    "закрывает то, что старше суток, а здесь — за последние сутки.",
        count=count, link="/report/rows/reconciliation_review",
        details=[line(r) for r in rows],
    )



def _check_backup_mirror(db: Session) -> Finding | None:
    """Копия снимается, но на вторую площадку не доезжает.

    Зеркало (`BACKUP_MIRROR_DIR` — папка облака, сетевая шара, второй диск)
    заводится ровно против одного случая: отказ диска, на котором лежат И база,
    И все копии. Пока зеркало молча не работает, этот случай снова не прикрыт —
    а выглядит всё исправным: копия снимается, проверяется, `/health` зелёный,
    находки «свежей копии нет» тоже нет.

    Сам бэкап из-за недоступного зеркала неудачным НЕ считается, и правильно:
    локальная копия снята и прочитана. Поэтому текст живёт в `last_error`
    успешной отметки, а читатель у него — здесь.
    """
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "backup",
        WorkerHeartbeat.last_success.is_(True),
    ).first()
    if row is None or not row.last_error:
        return None
    return Finding(
        key="backup_mirror", level=WARNING,
        title=f"Копия базы не уходит на вторую площадку: {row.last_error}",
        consequence="Локальная копия есть, но лежит на том же диске, что и база. "
                    "Отказ этого диска унесёт разом и базу, и все копии — ровно "
                    "то, против чего заводилась вторая площадка.",
        count=1, link="/diagnostics#workers",
    )


def _check_backup_missing(db: Session) -> Finding | None:
    """Свежей копии базы нет.

    Единственная находка отчёта, которая говорит не о том, что уже разошлось, а
    о том, чем кончится СЛЕДУЮЩАЯ неприятность. Аудит 21.09: боевую базу не
    копировал никто, а в ней лежит всё, что не восстанавливается ниоткуда —
    соответствие баркодов товарам (собиралось руками), история проведённых
    заказов, задания 1С с ответами. Ни 1С, ни площадки этого не знают.

    Порог — двое суток при суточном расписании: одна пропущенная копия может
    быть перезапуском воркера, две подряд означают, что механизм встал.

    База не SQLite — находки нет вовсе: у PostgreSQL свой механизм, и делать
    вид, что мы прикрыли и его, опаснее, чем молчать.
    """
    from app.backup import database_path, last_backup

    if database_path() is None:
        return None

    # Задание бэкапа ещё ни разу не отрабатывало — значит это свежая установка
    # или воркер только что поднялся. Ругаться тут нельзя: «отработало ли
    # задание» — вопрос `/health`, а не отчёта, и там он задан
    # (`REQUIRED_WORKERS["backup"]`). Молчание отчёта на исправной системе —
    # обязательное свойство: ругать установку за то, что она свежая, верный
    # способ приучить оператора пролистывать отчёт.
    beat = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "backup").first()
    if beat is None:
        return None

    moment, total = last_backup()
    if moment is not None and now_utc() - moment < BACKUP_STALE:
        return None

    if moment is None:
        title = "Резервной копии базы нет ни одной"
        level = CRITICAL
    else:
        title = f"Последней копии базы {_age(moment)} (копий всего {total})"
        level = CRITICAL if now_utc() - moment > BACKUP_STALE * 3 else WARNING
    return Finding(
        key="backup_missing", level=level, title=title,
        consequence="В базе лежит то, чего нет больше нигде: мэппинг баркодов, "
                    "история проведённых заказов, задания 1С. Потеря файла — это "
                    "не «откатимся на вчера», а ручная настройка каталога заново. "
                    "Копия снимается на живой базе, останавливать ничего не нужно.",
        count=1, link="/diagnostics#workers",
    )


def _check_orders_not_processed(db: Session) -> Finding | None:
    """Опрос заказов не проводит заказы — и рапортует «ок».

    Приём заказов ловит исключение по КАЖДОМУ заказу отдельно (иначе одна
    сбойная строка обрывала бы весь проход), делает `db.rollback()` и идёт
    дальше. Откат — обязателен, но он уносит и `SyncAnomaly`, и
    `ProcessedOrder`: персистентного следа не остаётся НИГДЕ, кроме строки лога,
    которая живёт до ротации. Предохранитель при этом сбрасывается намеренно —
    гасить кабинет из-за одной битой строки хуже.

    Отсюда состояние, в котором зелено всё: `/health` 200, «Диагностика» без
    бейджей, отчёт молчит, — а заказы не проводятся. Следствие прямое: единица
    продана на площадке, у нас не списана, перемещения в 1С нет, остаток завышен
    и уезжает наружу.

    Читаем `last_error` УСПЕШНОГО heartbeat — тот же приём, что у
    `_check_suspicious_snapshot`. Разовый сбой стирается следующим циклом через
    45 секунд, и находки не будет: она про устойчивый отказ, а не про мигание.
    """
    rows = [r for r in db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name.like("poll_orders_account_%"),
        WorkerHeartbeat.last_success.is_(True),
    ).all() if r.last_error]
    if not rows:
        return None
    return Finding(
        key="orders_not_processed", level=CRITICAL,
        title=f"Заказы не проводятся, кабинетов: {len(rows)}",
        consequence="Заказ на площадке есть, у нас он не проведён: остаток не "
                    "списан, перемещения в 1С нет. Остаток завышен ровно на эти "
                    "продажи и уезжает на площадки — прямой оверселл. Само не "
                    "пройдёт: цикл повторяет ту же строку каждые 45 секунд.",
        count=len(rows), link="/diagnostics#workers",
        details=[f"{r.worker_name}: {(r.last_error or '')[:200]}" for r in rows[:10]],
    )


def _check_verify_stock_broken(db: Session) -> Finding | None:
    """Сверка остатков не прошла по кабинету — и прогон выглядел чистым.

    `job_verify_stock` выбирал ветку по `diverged`, а он ноль и когда не
    проверено НИЧЕГО. Сверка — единственное, что ловит перезапись наших
    остатков второй системой; её тишина и её чистый результат выглядели
    одинаково.
    """
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "verify_stock",
        WorkerHeartbeat.last_success.is_(True),
    ).first()
    if row is None or not row.last_error:
        return None
    return Finding(
        key="verify_stock_broken", level=WARNING,
        title=f"Сверка остатков: {row.last_error}",
        consequence="По этим кабинетам никто не проверяет, осталось ли на "
                    "площадке наше число. Расхождение с площадкой не будет "
                    "найдено вовсе — а это единственная проверка, которая ловит "
                    "перезапись наших остатков второй системой.",
        count=1, link="/diagnostics#workers",
    )


def _check_ftp_receive_unmatched(db: Session) -> Finding | None:
    """1С ответила, а мы не поняли — к чему.

    Ответ, не легший ни на одно задание, УХОДИТ В АРХИВ и не возвращается: из
    архива их никто не перечитывает, а повторно 1С его не пришлёт. Задание, к
    которому он относился, доживает до `timeout` и навсегда считается «в пути».

    Найти такое задание можно (`_check_stuck_1c_tasks`, ручной разбор), но в
    разборе человек отвечает ровно на один вопрос — «создала 1С документ или
    нет», — и потерянный ответ и есть ответ на него. До сих пор этот факт жил
    только в логе, то есть был виден тому, кто пришёл его искать.

    Туда же строки выгрузки на дату, не легшие ни на одну заявку: по ним остаток
    ЦС на дату не проставится, и товары останутся ждать 1С, которая уже
    ответила."""
    row = db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name == "ftp_receive",
        WorkerHeartbeat.last_success.is_(True),
    ).first()
    if row is None or not row.last_error:
        return None
    return Finding(
        key="ftp_receive_unmatched", level=WARNING,
        title=f"Канал 1С: {row.last_error}",
        consequence="Ответ 1С потерян безвозвратно — он уже в архиве, и повторно "
                    "его не пришлют. Задание, к которому он относился, уйдёт в "
                    "«просрочено» и навсегда будет считаться «в пути»: по "
                    "созданиям остаток занижен и наружу уходит меньше, чем есть, "
                    "по отменам завышен — и это прямой оверселл.",
        count=1, link="/diagnostics#workers",
    )


def _check_truncated_catalog(db: Session) -> Finding | None:
    """Выгрузку каталога оборвал защитный предел страниц.

    Снимок при этом выглядит свежим: `fetched_at` у попавших обновлён, счётчик
    показывает «загружено N», `_check_stale_catalog` смотрит `max(fetched_at)` и
    претензий не имеет. Цена отложенная: по снимку считаются ключи отправки —
    chrtId у WB, variant_id у Kit, артикул у Ozon, — и позиция, не попавшая в
    огрызок, ключа не получит. У WB остаток уйдёт баркодом (приём которого WB
    умеет отключить), у Kit и Ozon не уйдёт вовсе и закроется терминально.

    Признак вычислялся и уезжал в heartbeat, но показать его было некому:
    «Диагностика» рисует по кабинету только `poll_orders_account_*` и пять общих
    имён, а `/health` при `last_success=True` текст не отдаёт.
    """
    rows = [r for r in db.query(WorkerHeartbeat).filter(
        WorkerHeartbeat.worker_name.like("catalog_poll_account_%"),
        WorkerHeartbeat.last_success.is_(True),
    ).all() if r.last_error]
    if not rows:
        return None
    return Finding(
        key="truncated_catalog", level=WARNING,
        title=f"Каталог выгружен не полностью, кабинетов: {len(rows)}",
        consequence="Снимок каталога неполон, а по нему считаются ключи "
                    "отправки. По не попавшим в него карточкам остаток уйдёт не "
                    "тем ключом или не уйдёт вовсе — и выглядеть это будет как "
                    "«нет карточки», а не как обрыв выгрузки.",
        count=len(rows), link="/diagnostics#workers",
        details=[f"{r.worker_name}: {(r.last_error or '')[:200]}" for r in rows[:10]],
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
    _check_stuck_broadcast_requests,
    _check_wb_without_chrt,
    _check_breaker_disabled,
    _check_stuck_1c_tasks,
    _check_stale_catalog,
    _check_stale_reconciliation,
    _check_suspicious_snapshot,
    _check_dispatch_stuck,
    _check_open_anomalies,
    _check_open_stock_date,
    _check_mapping_conflicts,
    _check_catalog_cards_without_1c,
    _check_negative_stock,
    _check_reconciliation_review,
    _check_backup_missing,
    _check_backup_mirror,
    _check_orders_not_processed,
    _check_verify_stock_broken,
    _check_ftp_receive_unmatched,
    _check_truncated_catalog,
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


def _rows_platform_divergence(db: Session) -> list[list[str]]:
    rows = _q_platform_divergence(db)
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
            names.get(r.account_id, str(r.account_id)),
            r.sent_sku or "",
            str(r.sent_quantity if r.sent_quantity is not None else ""),
            str(r.verified_quantity if r.verified_quantity is not None else ""),
            _age(r.verified_at),
        ])
    return out


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


def _rows_reconciliation_review(db: Session) -> list[list[str]]:
    rows = _q_reconciliation_review(db)
    products = {p.uid_1c: p for p in db.query(Product).filter(
        Product.uid_1c.in_({r.uid_1c for r in rows})).all()} if rows else {}
    out = []
    for r in rows:
        p = products.get(r.uid_1c)
        out.append([
            (p.article if p else "") or r.uid_1c,
            (p.size if p else "") or "", (p.color if p else "") or "",
            (p.name if p else "") or "",
            r.checked_at.strftime("%d.%m.%Y %H:%M") if r.checked_at else "",
            str(r.python_stock), str(r.actual_1c), f"{r.delta:+d}",
        ])
    return out


def _rows_negative_stock(db: Session) -> list[list[str]]:
    return [[p.article or p.uid_1c, p.size or "", p.color or "", p.name or "",
             str(p.stock_on_hand or 0)]
            for p in _q_negative_stock(db)]


def _rows_stuck_broadcast_requests(db: Session) -> list[list[str]]:
    return [[p.article or p.uid_1c, p.size or "", p.color or "", p.name or "",
             str(p.stock_on_hand or 0)]
            for p in _q_stuck_broadcast_requests(db)]


QUEUE_COLUMNS = ["Артикул", "Размер", "Цвет", "Наименование", "Кабинет",
                 "SKU, которым ушло", "Количество", "Что ответила площадка"]
PRODUCT_COLUMNS = ["Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС"]

# key находки → (заголовок страницы, колонки, функция строк).
# Что означает находка и ЧТО С НЕЙ ДЕЛАТЬ — по строке на каждый полный список.
#
# Страница показывала таблицу и подпись «Полный список строк находки»: человек
# видел, ЧТО нашлось, и не видел ни следствия, ни следующего шага. А приходят
# сюда как раз за вторым: находка сказала «разобрать →», человек нажал — и
# упёрся в список, по которому непонятно, что делать. Следствие при этом уже
# сформулировано в самой находке, но до этой страницы не доезжало.
#
# Правило то же, что у `Finding`: сначала СЛЕДСТВИЕ («чем это обернётся, если не
# трогать»), потом действие. Не формулируется следствие — значит это не находка,
# а просто число, и полного списка ей не нужно.
FULL_LIST_GUIDANCE = {
    "dispatch_errors": (
        "Остаток у нас уже списан, а площадка продолжает продавать по старому "
        "числу: это прямой оверселл, и сам он не рассосётся — запись в очереди "
        "терминальна, повторов по ней больше не будет.",
        "Посмотрите причину отказа в последней колонке. Отказ в связи или в "
        "ключах — «API-ключи»; отказ площадки по конкретной позиции — «Мэппинг». "
        "Разобравшись, нажмите «Переотправить остаток» на «Диагностике»."),
    "unknown_sku": (
        "Оверселла тут НЕ будет: продавать нечего, карточки на площадке нет. "
        "Зато товар не продаётся вовсе и молчит об этом — рассылке отправлять "
        "не по чему, заказов не будет.",
        "Это мэппинг, а не связь. Заведите карточку в кабинете площадки либо "
        "поправьте привязку баркода на «Мэппинге», затем обновите каталог "
        "кабинета — после него пара поднимется сама."),
    "wb_without_chrt": (
        "СЕГОДНЯ остаток по этим позициям уходит и доезжает — баркодом. Но WB "
        "умеет приём по баркоду отключить (`400 SKUUploadDisabled`), и в этот "
        "день карточки перестанут получать остаток МОЛЧА: отказа не будет, "
        "просто число перестанет обновляться.",
        "chrtId берётся из каталога кабинета. Нажмите «Загрузить каталог» на "
        "«Мэппинге» по этому кабинету. Если после загрузки строка осталась — "
        "карточки с такой характеристикой в кабинете нет, и это уже мэппинг."),
    "broadcast_without_recalc": (
        "Остаток по этим товарам уходит наружу, НЕ сверенный с продажами "
        "площадок: продажи с базовой даты в 1С не проведены, и наружу уезжает "
        "больше, чем есть.",
        "Отметьте эти строки на «Товарах» и нажмите «Расчёт». Пока он не "
        "прошёл, трансляцию по ним лучше выключить."),
    "negative_stock": (
        "Отрицательный остаток в 1С значит, что продали больше, чем числилось: "
        "либо приход не проведён, либо списание прошло дважды. Наружу по такой "
        "строке уходит ноль, то есть товар не продаётся.",
        "Разбирается в 1С, а не здесь: найдите недостающий приход или лишнее "
        "списание. Наша сверка перепишет остаток сама, как только 1С отдаст "
        "верное число."),
    "stuck_broadcast_requests": (
        "Оператор попросил включить трансляцию файлом, гейт согласился и "
        "отложил — а условие так и не выполнилось. Товар молчит уже сутки, и "
        "человек об этом не знает: он считает, что настроил всё файлом.",
        "Откройте строку на «Товарах» и посмотрите состояние расчёта: чаще "
        "всего это расчёт, закончившийся с проблемами, или кабинет, погашенный "
        "предохранителем. Устраните причину — просьба сработает сама."),
    "platform_divergence": (
        "Площадка держит НЕ ТО число, которое мы отправили. Значит в кабинет "
        "пишет кто-то ещё, и выигрывает написавший последним: наш остаток там "
        "живёт до чужой записи.",
        "Сверка ничего не чинит намеренно — автопереотправка при живом втором "
        "писателе превратилась бы в гонку. Сначала найдите, кто ещё пишет в "
        "кабинет, и остановите его; потом «Переотправить остаток»."),
    "reconciliation_review": (
        "Склад разошёлся с 1С больше, чем на порог. Остаток по этим строкам "
        "мы УЖЕ переписали по 1С — вопрос не в том, что делать с числом, а в "
        "том, почему оно разошлось.",
        "Разбирается в 1С. Кнопка «Закрыть старые расхождения» на «Диагностике» "
        "этим строкам не поможет: она берёт то, что СТАРШЕ суток, а здесь "
        "последние сутки."),
}

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
    "stuck_broadcast_requests": ("Просьбы включить трансляцию, не выполненные за сутки",
                                 PRODUCT_COLUMNS, _rows_stuck_broadcast_requests),
    "platform_divergence": ("Площадка держит не то, что мы отправили",
                           ["Артикул", "Размер", "Цвет", "Наименование", "Кабинет",
                            "Чем адресовали", "Отправили", "Площадка держит",
                            "Когда сверяли"], _rows_platform_divergence),
    "reconciliation_review": ("Крупные расхождения со складом 1С за сутки",
                              ["Артикул", "Размер", "Цвет", "Наименование",
                               "Когда сверяли", "Было у нас", "Стало по 1С",
                               "Разница"], _rows_reconciliation_review),
}
