"""Единая страница «Товары и остатки» (/products).

Заменила две прежние — «Синхронизируемые товары» (отбор по кабинетам) и
«Управление остатками» (сколько передавать). Разделение было источником ошибок:
выключателей три и живут они на разных уровнях, а оператор видел их порознь и
считал, что товар транслируется, хотя на площадки уходили нули.

Здесь всё в одной строке, и главное — по каждому кабинету показано ЧИСЛО,
которое реально уйдёт, а если ноль, то прямая причина и что нажать. Расчёт
берётся из `app/transmit.py` — того же модуля, которым считает рассылка.
"""

from datetime import date, datetime

from fastapi import APIRouter, Request, Depends, Form, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import (Barcode, Platform, PlatformAccount, PlatformCatalogItem,
                        Product, SyncSetting, User)
from app.flash import set_flash, pop_flash
from app.audit import log_action
from app.timeutils import now_utc, today_local
from app.excel_utils import (build_xlsx_response, read_xlsx_rows, read_upload, parse_bool_ru,
                             ExcelReadError, YES_NO)
from app.transmit import (explain, sku_quantity, enqueue_full_resend, enqueue_withdrawal,
                          should_withdraw, ever_transmitted,
                          offset_from_base, recompute_offset, sku_mode, MODE_AUTO,
                          covered_accounts)
from app.offset_base import (ensure_snapshot_requested, set_base_date, stock_at_date,
                              stock_lookup)
from app.recalc import active_job, create_job, last_job
from app.broadcast_gate import (DEFERRABLE_CALC_STATUSES, enabled_account_ids,
                                calc_status as _calc_status,
                                blocks_broadcast_on as _blocks_broadcast_on)

router = APIRouter()
templates = shared_templates


def _active_accounts(db: Session) -> list[PlatformAccount]:
    return db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)) \
        .order_by(PlatformAccount.platform, PlatformAccount.name).all()


def _account_label(account: PlatformAccount) -> str:
    return f"{account.name} ({account.platform.value.upper()})"


def _get_product(db: Session, uid_1c: str) -> Product | None:
    return db.query(Product).options(
        joinedload(Product.sync_settings), joinedload(Product.barcodes),
    ).filter(Product.uid_1c == uid_1c).first()


def _repropagate(db: Session, product: Product, reason: str = "manual_enable"):
    """После правки резерва/порога/трансляции — переотправить актуальное значение
    на отмеченные кабинеты, не дожидаясь следующего изменения остатка."""
    for setting in product.sync_settings:
        if setting.enabled:
            enqueue_full_resend(db, product.uid_1c, setting.account_id, reason=reason)


# Пустая ячейка в файле импорта НИЧЕГО НЕ МЕНЯЕТ, и это правило общее на все
# колонки. Раньше каждая понимала пустоту как самое разрушительное значение из
# возможных: «Трансляция» и «<кабинет> — Синхронизировать» читались как «Нет»,
# то есть снимали галочку И ОТЗЫВАЛИ остаток (ноль на живую карточку площадки);
# «<кабинет> — Порог» становился нулём; «Порог трансляции» стирался, возвращая
# на площадки полный остаток; «Дата расчёта» снимала расчёт вместе с ФАКТОМ,
# который человек получил, пересчитав склад руками. Выгрузка все эти ячейки
# заполняет, поэтому пустой ячейка становится ровно в двух случаях: её стёрли
# или файл собран не из нашей выгрузки, — и ни один из них не значит «примени
# ко всему файлу самое опасное». Снять значение по-прежнему можно, но сказав об
# этом вслух: «-» в ячейке.
CLEAR_CELL = ("-", "--", "—", "–")


def _cell_intent(value):
    """Что означает ячейка: (`действие`, `текст`).

    `skip` — пусто, не трогаем. `clear` — явное «-», снимаем. `set` — значение.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return "skip", ""
    if text in CLEAR_CELL:
        return "clear", ""
    return "set", text


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _stamp_active_since(db: Session, uid_1c: str) -> None:
    """Проставить «Активно с» текущей датой при первой отметке кабинета.

    Отметка кабинета и есть момент, с которого товар начинает жить на площадке,
    и дату этого момента оператор раньше вписывал руками — то есть забывал.
    Заполняем ТОЛЬКО пустое: если дата уже стоит, она отмечает первое включение,
    и перетирать её повторной отметкой значило бы терять историю.
    """
    product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
    if product is not None and product.broadcast_active_since is None:
        product.broadcast_active_since = today_local()


def _uids_with_barcode(db: Session, products: list[Product]) -> set[str]:
    """У кого из показанных товаров есть хоть один баркод — ОДНИМ запросом.

    Раньше это знание приезжало через `joinedload(Product.barcodes)`: к каждому
    товару подтягивались все его баркоды, хотя строке нужен один-единственный
    факт «есть или нет». На каталоге в 152 тысячи товаров и 154 тысячи баркодов
    это лишний join на каждой загрузке страницы.
    """
    if not products:
        return set()
    uids = [p.uid_1c for p in products]
    return {row[0] for row in
            db.query(Barcode.uid_1c).filter(Barcode.uid_1c.in_(uids)).distinct().all()}


def _row(product: Product, accounts: list[PlatformAccount],
         all_accounts: dict[int, PlatformAccount] | None = None,
         with_barcode: set[str] | None = None) -> dict:
    """Строка таблицы. По каждому кабинету — реальное число и причина нуля."""
    settings_map = {s.account_id: s for s in product.sync_settings}
    all_accounts = all_accounts or {a.id: a for a in accounts}

    sku = explain(product, None, None)          # уровень SKU: только выключатель трансляции
    per_account = {}
    for account in accounts:
        setting = settings_map.get(account.id)
        result = explain(product, setting, account)
        per_account[account.id] = {
            "enabled": setting.enabled if setting else False,
            "has_proposal": setting.has_proposal if setting else False,
            "proposal_date": setting.proposal_date if setting else None,
            "min_threshold": setting.min_threshold if setting else 0,
            # Порог кабинета применяется ТОЛЬКО в автоматическом режиме. При
            # заданном пороге трансляции он молча ни на что не влияет — и так же
            # молча срабатывает, если порог трансляции потом сбросить. Строка
            # обязана показывать это состояние, иначе число выглядит рабочим.
            "threshold_active": sku_mode(product) == MODE_AUTO,
            "quantity": result.quantity,
            "potential": result.potential,
            "blocked": result.blocked,
            "reason": result.reason,
            "fix_hint": result.fix_hint,
        }

    # Предложения ⚡ рядом с наименованием: колонки кабинетов уезжают вправо за край
    # экрана, и оператор, включив фильтр «только с предложениями», не понимал, где
    # молния. Заодно видно предложение по кабинету, который сейчас неактивен и
    # колонки на странице не имеет.
    proposals = []
    for setting in product.sync_settings:
        if not setting.has_proposal or setting.enabled:
            continue
        account = all_accounts.get(setting.account_id)
        proposals.append({
            "account_id": setting.account_id,
            "name": account.name if account else f"кабинет #{setting.account_id}",
            "shown": any(a.id == setting.account_id for a in accounts),
            "date": setting.proposal_date,
        })
    proposals.sort(key=lambda p: p["name"])

    # Кабинеты уже загружены (joinedload) — лишнего запроса на строку не будет.
    # Только ЖИВЫЕ кабинеты — тот же набор, по которому ворота решают, можно ли
    # включать трансляцию (`broadcast_gate.enabled_account_ids`). Разойдись они,
    # строка показывала бы «актуализирован», а включение не срабатывало.
    enabled_ids = enabled_account_ids(product)
    has_cabinet = bool(enabled_ids)

    return {
        "uid_1c": product.uid_1c, "article": product.article, "name": product.name,
        "proposals": proposals,
        "size": product.size, "color": product.color,
        "stock_on_hand": product.stock_on_hand or 0, "reserve": product.reserve or 0,
        "broadcast_offset": product.broadcast_offset,
        # Расчёт порога от даты. `waiting_for_1c` — дата задана, а ответа ещё нет:
        # строка не сломана, она просто ждёт файл, и оператору надо видеть именно
        # это, а не пустой порог без объяснения.
        "base_date": product.offset_base_date,
        "base_stock": product.offset_base_stock,
        "fact_at_date": product.fact_at_date,
        "computed_offset": offset_from_base(product),
        "waiting_for_1c": (product.offset_base_date is not None
                           and product.offset_base_stock is None),
        "calc_status": _calc_status(product, has_cabinet, enabled_ids)[0],
        "calc_status_label": _calc_status(product, has_cabinet, enabled_ids)[1],
        "transmit_override": product.transmit_override,   # legacy: только предупреждение
        "broadcast_enabled": product.broadcast_enabled,
        # Просьба из файла Excel: трансляция включится сама, когда расчёт
        # поставит отметку. Без этой подписи ожидание невидимо — строка просто
        # стоит выключенной, и оператор включает её руками второй раз.
        "broadcast_requested": product.broadcast_requested_at is not None,
        "active_since": product.broadcast_active_since,
        # `with_barcode` — готовый ответ на всю страницу. Без него спрашиваем
        # сам объект: так зовут одиночные пути (перерисовка одной строки), и
        # там joinedload на месте.
        "has_barcode": (product.uid_1c in with_barcode if with_barcode is not None
                        else len(product.barcodes) > 0),
        "sku_quantity": sku_quantity(product),
        "sku_blocked": sku.blocked,
        "sku_reason": sku.reason,
        "accounts": per_account,
    }


PAGE_LIMIT = 300          # без фильтров: «первые 300 из 152 тысяч по алфавиту» —
                          # витрина, работают всегда через отбор
# С ФИЛЬТРАМИ показываем больше, но не «весь отбор»: строка весит около 6 КБ
# (дата, факт, бронь плюс по несколько полей на каждый из пяти кабинетов), и
# пять тысяч строк — это 32 МБ разметки. Столько не успевает ни сервер собрать,
# ни браузер разложить: именно так выглядела «программа долго отрабатывает».
# Замер 21.09 на боевом каталоге: 300 строк — 1,9 МБ и полсекунды, пять тысяч —
# минуты.
#
# Массовой правке этот потолок не мешает НИКАК: галочка в шапке берёт весь отбор
# целиком (`BULK_LIMIT`), сколько бы строк ни было показано, и страница об этом
# прямо пишет. Экспорт в Excel тоже отдаёт всё.
FILTERED_LIMIT = 500
EXPORT_LIMIT = 50000      # потолок выгрузки: защита от попытки собрать .xlsx на весь каталог
# Потолок массовой правки «по всему фильтру». Каждая строка тянет за собой запись
# в очередь рассылки на каждый отмеченный кабинет, а база — SQLite, в которую в
# это же время пишет планировщик. Правка всего каталога разом заняла бы её
# минутами, и страница висела бы без признаков жизни.
BULK_LIMIT = 20000


def _base_query(db: Session, q: str, only_proposals: bool, only_blocked: bool,
                hide_size_u: bool = False, only_unfinished: bool = False,
                only_on_platform: bool = False, only_marked: bool = False,
                hide_zero_stock: bool = False, only_broadcasting: bool = False):
    """Отбор в SQL — ДО ограничения по количеству строк.

    Раньше сначала брались первые 300 товаров по алфавиту, и лишь потом
    применялись фильтры: при каталоге в 152 тысячи SKU «Только с предложениями»
    и «Только те, где уходит 0» показывали пусто, потому что в первых 300 по
    алфавиту таких товаров не было."""
    # `joinedload(Product.barcodes)` здесь БЫЛ и убран намеренно. Строке нужен от
    # баркодов ровно один факт — есть он или нет, — а join тянул к каждому товару
    # все его баркоды из таблицы на 154 тысячи строк и размножал результат. Факт
    # добирается одним запросом на страницу, см. `_uids_with_barcode`.
    query = db.query(Product).options(joinedload(Product.sync_settings))
    if q:
        like = f"%{q}%"
        conditions = [Product.article.ilike(like), Product.name.ilike(like)]
        # Штрихкод ищется наравне с артикулом и названием: оператор приходит сюда
        # со сканером или из кабинета площадки, где у позиции виден ИМЕННО он, —
        # и без этого поиска ему приходилось идти в «Мэппинг», там узнавать
        # товар, а потом искать его здесь заново.
        #
        # Спрашиваем баркоды ОДИН раз на запрос, а не на каждый товар. Раньше тут
        # стоял коррелированный EXISTS, и на боевом каталоге он означал буквально
        # следующее: 152 тысячи товаров, на каждый — чтение всех 154 тысяч
        # баркодов. Страница не отвечала вовсе, замер 21.09 на ней и завис.
        # `IN (подзапрос)` SQLite считает один раз и складывает во временный
        # индекс. JOIN тут не годится по-прежнему: баркодов у товара несколько,
        # и он размножил бы строку товара по числу совпавших.
        #
        # В цифрах баркода быть обязано. Поиск «куртка» иначе всё равно шёл бы по
        # таблице баркодов целиком — ради заведомо пустого результата.
        if any(ch.isdigit() for ch in q):
            conditions.append(Product.uid_1c.in_(
                db.query(Barcode.uid_1c).filter(Barcode.barcode.ilike(like))))
        query = query.filter(or_(*conditions))
    if only_proposals:
        query = query.filter(Product.sync_settings.any(SyncSetting.has_proposal.is_(True)))
    if only_blocked:
        # Предварительный отбор: «ничего не уходит» имеет смысл только для товаров,
        # отмеченных хотя бы в одном кабинете. Точный расчёт — ниже, по лестнице.
        query = query.filter(Product.sync_settings.any(SyncSetting.enabled.is_(True)))
    if hide_zero_stock:
        # «Скрыть товары с нулевым остатком ЦС». Отбор ровно по нулю, а не по
        # «больше нуля»: ОТРИЦАТЕЛЬНЫЙ остаток (пересортица) прятать нельзя —
        # это как раз то, что надо видеть и разбирать в 1С, и «нулевым» он не
        # является. Спрятать его здесь значило бы спрятать проблему.
        query = query.filter(Product.stock_on_hand != 0)
    if hide_size_u:
        # Безразмерные позиции (характеристика «U» — универсальный размер) оператору
        # в этом списке не нужны. Сравнение без учёта регистра и пробелов; товары
        # без размера не прячем — у них характеристики просто нет.
        query = query.filter(or_(Product.size.is_(None),
                                 func.upper(func.trim(Product.size)) != "U"))
    if only_unfinished:
        # Незавершённый расчёт порога: дата задана, но либо 1С ещё не ответила,
        # либо факт не введён. На каталоге в 152 тысячи SKU без этого фильтра
        # недоделанные строки просто не найти — а именно их и надо доделать.
        query = query.filter(
            Product.offset_base_date.isnot(None),
            or_(Product.offset_base_stock.is_(None), Product.fact_at_date.is_(None)),
        )
    if only_broadcasting:
        # Трансляция ВКЛЮЧЕНА у самого товара — то есть его остаток уходит
        # наружу прямо сейчас. Это не то же самое, что «отмечен кабинет»:
        # галочка кабинета говорит, КУДА передавать, а трансляция — передаём ли
        # вообще. Выключенная трансляция перекрывает любые галочки, и товар с
        # пятью отмеченными кабинетами молчит.
        #
        # Отбор нужен ровно затем, что «что сейчас уходит наружу» на каталоге в
        # 152 тысячи SKU иначе не посмотреть: включённых строк сотни, и найти их
        # среди остальных нечем.
        query = query.filter(Product.broadcast_enabled.is_(True))
    if only_marked:
        # Товар отмечен хотя бы в одном кабинете — то есть оператор УЖЕ решил,
        # что он туда передаётся. Это не то же самое, что «найден на площадке»:
        # там карточка просто существует, а здесь принято решение. Именно по
        # такому отбору смотрят, что реально уходит наружу, и правят его
        # массово.
        #
        # Кабинет не обязан быть активным по той же причине, что и в фильтре
        # выше: галочку ставил человек, и выключение кабинета его решения не
        # отменяет.
        query = query.filter(Product.sync_settings.any(SyncSetting.enabled.is_(True)))
    if only_on_platform:
        # Товар, чей баркод нашёлся в каталоге хоть одного кабинета, — то есть
        # карточка на площадке существует и подключать его есть куда. Обратное
        # («не найден») означает либо что каталог кабинета ещё не выгружали, либо
        # что карточки там правда нет, и различить это фильтром нельзя.
        #
        # Кабинет НЕ обязан быть активным. Каталог остаётся от кабинета, который
        # погасил предохранитель или выключили руками, и товар на площадке от
        # этого никуда не делся. Привязка к активности заставляла бы отбор
        # моргать вместе с состоянием кабинетов — фильтр должен отвечать на
        # вопрос о товаре, а не о нашей связи с площадкой.
        #
        # Коррелированный EXISTS, а не `IN (подзапрос)`: у `platform_catalog_items`
        # и `barcodes` баркод проиндексирован, и на каталоге в 152 тысячи SKU это
        # разница между поиском по индексу и вычиткой всей таблицы в память.
        query = query.filter(
            db.query(PlatformCatalogItem.id)
            .join(Barcode, Barcode.barcode == PlatformCatalogItem.barcode)
            .filter(Barcode.uid_1c == Product.uid_1c)
            .exists()
        )
    return query.order_by(Product.name)


def _blocked_everywhere(product: Product, accounts: list[PlatformAccount]) -> bool:
    """Ни один отмеченный кабинет не получает положительное число."""
    settings_map = {s.account_id: s for s in product.sync_settings}
    marked = [a for a in accounts if (settings_map.get(a.id) and settings_map[a.id].enabled)]
    return bool(marked) and all(explain(product, settings_map.get(a.id), a).quantity == 0 for a in marked)


def _load_products(db: Session, q: str, only_proposals: bool, only_blocked: bool,
                   accounts: list[PlatformAccount], limit: int = PAGE_LIMIT,
                   hide_size_u: bool = False,
                   only_unfinished: bool = False,
                   only_on_platform: bool = False,
                   only_marked: bool = False,
                   hide_zero_stock: bool = False,
                   only_broadcasting: bool = False) -> tuple[list[Product], int]:
    """Возвращает (строки, сколько всего подходит под фильтр). Второе число нужно,
    чтобы честно написать оператору «показано 300 из N», а не делать вид, что это всё.
    Отрицательное значение = счёт оборван на пределе сканирования, в интерфейсе
    показывается как «N+»."""
    query = _base_query(db, q, only_proposals, only_blocked, hide_size_u,
                        only_unfinished, only_on_platform, only_marked,
                        hide_zero_stock, only_broadcasting)

    if not only_blocked:
        # `enable_eagerloads(False)`: считаем строки, а не собираем объекты.
        # Без этого SQLAlchemy заворачивает в подсчёт и join настроек кабинетов —
        # на каталоге в 152 тысячи это лишняя работа ровно впустую.
        total = query.order_by(None).enable_eagerloads(False).count()
        return query.limit(limit).all(), total

    # «Уходит 0» точно считается только лестницей: идём порциями и останавливаемся,
    # набрав limit, чтобы не тянуть весь каталог в память. Счёт «всего» тоже
    # ограничен: на каталоге в сотни тысяч SKU точный пересчёт стоил бы секунд.
    kept, total, offset = [], 0, 0
    CHUNK, SCAN_CAP = 1000, 20000
    partial = False
    while True:
        chunk = query.offset(offset).limit(CHUNK).all()
        if not chunk:
            break
        for p in chunk:
            if _blocked_everywhere(p, accounts):
                total += 1
                if len(kept) < limit:
                    kept.append(p)
        offset += CHUNK
        if len(kept) >= limit and offset >= SCAN_CAP:
            partial = True          # «из N+»: дальше не считали
            break
    return kept, (-total if partial else total)


def _dispatch_summary(db: Session) -> dict:
    """Для мастер-переключателей: по каждой площадке — сколько кабинетов транслируют."""
    by_platform: dict[str, dict] = {}
    for a in db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True)).all():
        d = by_platform.setdefault(a.platform.value, {"platform": a.platform.value, "total": 0, "on": 0})
        d["total"] += 1
        if a.dispatch_enabled:
            d["on"] += 1
    return {
        "platforms": list(by_platform.values()),
        "all_total": sum(d["total"] for d in by_platform.values()),
        "all_on": sum(d["on"] for d in by_platform.values()),
    }


def _render(request: Request, db: Session, user: User, q: str, only_proposals: bool,
            only_blocked: bool, hide_size_u: bool, template: str,
            only_unfinished: bool = False, only_on_platform: bool = False,
            only_marked: bool = False, hide_zero_stock: bool = False,
            only_broadcasting: bool = False):
    accounts = _active_accounts(db)
    all_accounts = {a.id: a for a in db.query(PlatformAccount).all()}
    # С фильтрами показываем ВЕСЬ отбор: оператор сузил список именно затем,
    # чтобы увидеть его целиком. Без фильтров — витрина в 300 строк: «первые 300
    # из 152 тысяч по алфавиту» всё равно ни о чём не говорят.
    filtered = bool(q or only_proposals or only_blocked or hide_size_u
                    or only_unfinished or only_on_platform or only_marked
                    or hide_zero_stock or only_broadcasting)
    products, total = _load_products(db, q, only_proposals, only_blocked, accounts,
                                     limit=FILTERED_LIMIT if filtered else PAGE_LIMIT,
                                     hide_size_u=hide_size_u,
                                     only_unfinished=only_unfinished,
                                     only_on_platform=only_on_platform,
                                     only_marked=only_marked,
                                     hide_zero_stock=hide_zero_stock,
                                     only_broadcasting=only_broadcasting)
    with_barcode = _uids_with_barcode(db, products)
    rows = [_row(p, accounts, all_accounts, with_barcode) for p in products]
    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "products",
        "rows": rows, "total": total, "page_limit": PAGE_LIMIT,
        "q": q, "only_proposals": only_proposals, "only_blocked": only_blocked,
        "hide_size_u": hide_size_u, "hide_zero_stock": hide_zero_stock,
        "only_unfinished": only_unfinished,
        "only_on_platform": only_on_platform,
        "only_marked": only_marked,
        "only_broadcasting": only_broadcasting,
        "recalc_job": active_job(db) or last_job(db),
        "recalc_running": active_job(db) is not None,
        "accounts": accounts,
        "dispatch": _dispatch_summary(db),
        "flash": pop_flash(request) if template == "products.html" else None,
    })


def _row_response(request: Request, db: Session, uid_1c: str,
                  error: str = "", error_field: str = ""):
    """Ответ HTMX: перерисованная строка (все числа пересчитываются заново).

    `error` — текст ошибки ввода, `error_field` — у какого поля его показать
    (`reserve`, `offset`, `active_since`, `threshold:<id кабинета>`; пусто —
    рядом с наименованием).

    Ошибку показываем ПРЯМО В СТРОКЕ, а не через флеш-сообщение. Флеш снимается
    только при полной перезагрузке страницы, а здесь ответ — фрагмент: строка
    молча перерисовывалась прежним значением, и оператор считал, что ввод принят,
    а плашка всплывала позже и уже без контекста."""
    accounts = _active_accounts(db)
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    all_accounts = {a.id: a for a in db.query(PlatformAccount).all()}
    return templates.TemplateResponse(request, "products_row.html", {
        "request": request, "row": _row(product, accounts, all_accounts), "accounts": accounts,
        "error": error, "error_field": error_field,
    })


def _as_int(raw: str) -> int | None:
    """Целое из поля формы или None. Браузер в <input type="number"> отдаёт пустую
    строку, если введено что-то нечисловое («12,5» с запятой — самый частый
    случай), поэтому поля принимаем строкой и разбираем сами: с `int = Form(...)`
    FastAPI отвечал 422, htmx при таком ответе строку не подменяет вовсе, и
    оператор не видел ни нового значения, ни ошибки."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _filter_query(q: str, only_proposals: bool = False, only_blocked: bool = False,
                  hide_size_u: bool = False, only_unfinished: bool = False,
                  only_on_platform: bool = False, only_marked: bool = False,
                  hide_zero_stock: bool = False,
                  only_broadcasting: bool = False) -> str:
    """Фильтры страницы в виде строки запроса."""
    from urllib.parse import urlencode

    params = {"q": q} if q else {}
    for name, on in (("only_proposals", only_proposals), ("only_blocked", only_blocked),
                     ("hide_size_u", hide_size_u), ("only_unfinished", only_unfinished),
                     ("only_on_platform", only_on_platform),
                     ("only_marked", only_marked),
                     ("hide_zero_stock", hide_zero_stock),
                     ("only_broadcasting", only_broadcasting)):
        if on:
            params[name] = "true"
    return urlencode(params)


def _back(q: str, only_proposals: bool = False, only_blocked: bool = False,
          hide_size_u: bool = False, only_unfinished: bool = False,
          only_on_platform: bool = False, only_marked: bool = False,
          hide_zero_stock: bool = False,
          only_broadcasting: bool = False) -> RedirectResponse:
    """Назад на страницу С ТЕМИ ЖЕ ФИЛЬТРАМИ.

    Раньше возвращался только поиск: оператор отбирал строки фильтром «только
    незавершённый расчёт», применял массовую правку — и попадал на полный список,
    где отобранных строк уже не найти."""
    query = _filter_query(q, only_proposals, only_blocked, hide_size_u, only_unfinished,
                          only_on_platform, only_marked, hide_zero_stock,
                          only_broadcasting)
    return RedirectResponse(f"/products{'?' + query if query else ''}", status_code=303)


# --------------------------------------------------------------------------- страница

@router.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    only_blocked: bool = Query(False), hide_size_u: bool = Query(False),
    only_unfinished: bool = Query(False), only_on_platform: bool = Query(False),
    only_marked: bool = Query(False), hide_zero_stock: bool = Query(False),
    only_broadcasting: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if not _active_accounts(db):
        set_flash(request, "Пока нет ни одного активного кабинета — добавьте его на странице «API-ключи».", "warn")
    return _render(request, db, user, q, only_proposals, only_blocked, hide_size_u,
                   "products.html", only_unfinished=only_unfinished,
                   only_on_platform=only_on_platform, only_marked=only_marked,
                   hide_zero_stock=hide_zero_stock,
                   only_broadcasting=only_broadcasting)


@router.get("/products/rows", response_class=HTMLResponse)
def products_rows(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    only_blocked: bool = Query(False), hide_size_u: bool = Query(False),
    only_unfinished: bool = Query(False), only_on_platform: bool = Query(False),
    only_marked: bool = Query(False), hide_zero_stock: bool = Query(False),
    only_broadcasting: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, q, only_proposals, only_blocked, hide_size_u,
                   "products_rows.html", only_unfinished=only_unfinished,
                   only_on_platform=only_on_platform, only_marked=only_marked,
                   hide_zero_stock=hide_zero_stock,
                   only_broadcasting=only_broadcasting)


# Старые адреса — на новую страницу (в закладках и в переписке они ещё живут).
@router.get("/sync-products")
@router.get("/stock-control")
def legacy_redirect(q: str = Query("")):
    return RedirectResponse(f"/products{'?q=' + q if q else ''}", status_code=301)


# --------------------------------------------------------------------------- правки строки

@router.post("/products/{uid_1c}/reserve", response_class=HTMLResponse)
def set_reserve(
    request: Request, uid_1c: str, reserve: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Резерв: столько штук держим у себя. Применяется только в автоматическом
    режиме — заданный порог трансляции считает от остатка ЦС напрямую."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    parsed = _as_int(reserve)
    if parsed is None:
        return _row_response(request, db, uid_1c,
                             error="Резерв: введите целое число (без запятой).",
                             error_field="reserve")
    reserve = max(0, parsed)
    if product.reserve != reserve:
        product.reserve = reserve
        # Бронь входит в формулу порога, поэтому её смена обязана пересчитать
        # порог. Без этого новое значение брони осталось бы словами: в пороге
        # продолжала бы сидеть старая.
        recompute_offset(product)
        log_action(db, user.username, "reserve_changed", f"{uid_1c} -> {reserve}")
        _repropagate(db, product, reason="reserve_changed")
        db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/base-date", response_class=HTMLResponse)
def set_base_date_route(
    request: Request, uid_1c: str, value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Дата, на которую считается порог.

    Снимок 1С на эту дату уже есть — остаток подставится сразу. Нет — строка
    встанет в ожидание, и расчёт доделается сам, когда придёт файл ответа
    (`offset_base.fill_waiting_products`). Пустая дата снимает расчёт, но НЕ
    стирает порог: обнулить его здесь значило бы молча вернуть на площадки
    полный остаток."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    try:
        day = _parse_date(value)
    except ValueError:
        return _row_response(request, db, uid_1c,
                             error="Дата: формат ГГГГ-ММ-ДД.", error_field="base_date")
    if day is not None and day > today_local():
        return _row_response(request, db, uid_1c,
                             error="Остатков на будущую дату в 1С нет.", error_field="base_date")

    set_base_date(db, product, day)
    if day is not None:
        # Выгрузку на эту дату спрашиваем сами. Иначе связка дырявая: оператор
        # задал дату, а попросить у 1С срез должен не забыть руками на другой
        # странице — забудет, и строка навсегда останется в «ждём выгрузку».
        ensure_snapshot_requested(db, day, user.username)
    log_action(db, user.username, "offset_base_date_changed", f"{uid_1c} -> {value or 'снята'}")
    _repropagate(db, product, reason="offset_base_date")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/fact", response_class=HTMLResponse)
def set_fact(
    request: Request, uid_1c: str, value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Факт на дату: сколько лежало на складе НА САМОМ ДЕЛЕ.

    Пусто — оператор не вводил, берём остаток ЦС на дату, и порог сводится к
    брони. Ноль — утверждение «на складе пусто», это другое: тогда весь учётный
    остаток 1С считается расхождением. Отрицательным не бывает."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)

    raw = (value or "").strip()
    if raw:
        parsed = _as_int(raw)
        if parsed is None:
            return _row_response(request, db, uid_1c,
                                 error="Факт на дату: введите целое число (без запятой).",
                                 error_field="fact")
        product.fact_at_date = max(0, parsed)
    else:
        product.fact_at_date = None

    recompute_offset(product)
    log_action(db, user.username, "fact_at_date_changed", f"{uid_1c} -> {raw or 'сброшен'}")
    _repropagate(db, product, reason="fact_changed")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/offset", response_class=HTMLResponse)
def set_offset(
    request: Request, uid_1c: str, value: str = Form(""),
    available: str = Form(""), stock_at_date: str = Form(""), clear: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Порог трансляции (`Product.broadcast_offset`) — постоянное расхождение между
    учётом 1С и реальностью склада. На площадки уходит max(0, остаток ЦС − порог).

    Задаётся двумя способами: числом напрямую (`value`) или расчётом от пересчёта
    (`stock_at_date` − `available`, может быть отрицательным). Порог фиксирован:
    заказы и сверка двигают только остаток, поэтому цифра не дрейфует — в отличие
    от прежнего ручного остатка, который приходилось поправлять руками."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)

    if clear or (not value.strip() and not available.strip() and not stock_at_date.strip()):
        product.broadcast_offset = None
        # Снимаем и исходные три величины. Иначе «сброшен» было бы неправдой:
        # дата осталась бы на месте, и первая же правка брони вернула бы порог
        # обратно — оператор решил бы, что кнопка не работает.
        product.offset_base_date = None
        product.offset_base_stock = None
        product.fact_at_date = None
        detail = "сброшен (автоматический расчёт от остатка и резерва)"
    else:
        try:
            if value.strip():
                offset = int(value.strip())
            else:
                offset = int(stock_at_date.strip()) - int(available.strip())
        except ValueError:
            return _row_response(
                request, db, uid_1c,
                error="Порог: введите целое число (без запятой) либо оба числа пересчёта.",
                error_field="offset")
        product.broadcast_offset = offset
        product.transmit_override = None  # порог заменяет устаревшую ручную цифру
        detail = f"{offset}"

    log_action(db, user.username, "broadcast_offset_changed", f"{uid_1c} -> {detail}")
    _repropagate(db, product, reason="offset_changed")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/clear-override", response_class=HTMLResponse)
def clear_override(
    request: Request, uid_1c: str,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Сброс устаревшего ручного остатка. Задать его из интерфейса больше нельзя —
    остался только сброс для товаров, где он проставлен с прошлых версий."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    if product.transmit_override is not None:
        product.transmit_override = None
        log_action(db, user.username, "transmit_override_cleared", uid_1c)
        _repropagate(db, product, reason="override_cleared")
        db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/broadcast", response_class=HTMLResponse)
def toggle_broadcast(
    request: Request, uid_1c: str, enabled: bool = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Трансляция SKU — главный выключатель товара. Выключено → на площадки уходит 0
    независимо от порогов и остатка. По умолчанию выключена у всех товаров."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    if product.broadcast_enabled != enabled:
        if enabled:
            blocked = _blocks_broadcast_on(product)
            if blocked is not None:
                return _row_response(
                    request, db, uid_1c, error_field="broadcast",
                    error=f"Включать трансляцию рано: {blocked}. Сначала расчёт — "
                          f"иначе на площадки уйдёт остаток, не сверенный с их продажами.")
        product.broadcast_enabled = enabled
        # Выключили руками — просьба «включить после расчёта» снимается вместе с
        # галочкой. Иначе ближайший расчёт вернул бы трансляцию сам, молча и
        # вопреки только что сделанному выключению.
        product.broadcast_requested_at = None
        log_action(db, user.username, "broadcast_toggled", f"{uid_1c} -> {enabled}")
        if enabled:
            _repropagate(db, product, reason="broadcast_toggled")
        else:
            # Снятие с трансляции — ОСОЗНАННЫЙ отзыв: на площадку надо отправить
            # ноль, иначе она продолжит продавать по последнему присланному
            # числу. Раньше это работало побочным эффектом (ставили в очередь
            # обычную доотправку, а та считала ноль); теперь автоматические пути
            # такой товар вообще не ставят в очередь, поэтому отзыв делается явно.
            for setting in product.sync_settings:
                # `should_withdraw` здесь не годится: трансляцию мы уже
                # выключили, и она ответила бы «нечего» по всем кабинетам сразу.
                # Спрашиваем то единственное, что важно: уходил ли туда остаток.
                if setting.enabled and ever_transmitted(db, uid_1c, setting.account_id):
                    enqueue_withdrawal(db, uid_1c, setting.account_id,
                                       reason="broadcast_off")
        db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/active-since", response_class=HTMLResponse)
def set_active_since(
    request: Request, uid_1c: str, value: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Дата старта задним числом — от неё стартует бэкфилл на «Тестировании»."""
    product = _get_product(db, uid_1c)
    if product is None:
        return HTMLResponse("", status_code=404)
    try:
        product.broadcast_active_since = _parse_date(value)
    except ValueError:
        return _row_response(request, db, uid_1c,
                             error="Дата должна быть в формате ГГГГ-ММ-ДД.",
                             error_field="active_since")
    log_action(db, user.username, "active_since_changed", f"{uid_1c} -> {product.broadcast_active_since}")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/{account_id}/toggle", response_class=HTMLResponse)
def toggle_sync(
    request: Request, uid_1c: str, account_id: int, enabled: bool = Form(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Отметка кабинета: передавать ли этот товар в этот кабинет. Живой опрос
    заказов работает только по отмеченным парам товар+кабинет.

    Снятие галочки ОТЗЫВАЕТ остаток с площадки (ставит в очередь ноль). Иначе на
    площадке остаётся последнее отправленное число, она продолжает продавать, а
    заказы по снятой паре гейт отбора уже пропускает: ни списания у нас, ни
    документа в 1С. Главный выключатель товара всегда вёл себя именно так.

    **Но только если трансляция товара включена.** При выключенной трансляции по
    автоматическим путям наружу не уходило НИЧЕГО (`enqueue_full_resend` такой
    товар в очередь не ставит вовсе), отзывать нечего — а ноль, отправленный «на
    всякий случай», обнуляет чужую карточку, по которой идут продажи. Это ровно
    та асимметрия, из-за которой 18.09 на Озон и Kit уехали нули по товару,
    который мы туда ни разу не транслировали: доотправку почини́ли, а отзыв нет.

    Случай «транслировали, потом сняли галочку» не страдает: там трансляция
    включена, число на площадку уходило, и отзыв по-прежнему нужен."""
    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()
    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)

    was_enabled = setting.enabled
    setting.enabled = enabled
    if enabled and not was_enabled:
        setting.enabled_at = now_utc()
        setting.has_proposal = False
        _stamp_active_since(db, uid_1c)
        enqueue_full_resend(db, uid_1c, account_id)
        log_action(db, user.username, "sync_enabled", f"{uid_1c} / кабинет #{account_id}")
    elif not enabled and was_enabled:
        product = _get_product(db, uid_1c)
        if should_withdraw(db, product, account_id):
            enqueue_withdrawal(db, uid_1c, account_id)
            log_action(db, user.username, "sync_disabled", f"{uid_1c} / кабинет #{account_id} (в очередь 0)")
        else:
            log_action(db, user.username, "sync_disabled",
                       f"{uid_1c} / кабинет #{account_id} (на этот кабинет остаток не уходил — отзывать нечего)")
    db.commit()
    return _row_response(request, db, uid_1c)


@router.post("/products/{uid_1c}/{account_id}/threshold", response_class=HTMLResponse)
def set_threshold(
    request: Request, uid_1c: str, account_id: int, min_threshold: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Порог кабинета — страховой буфер: если доступное количество не больше него,
    на эту площадку уходит 0. 0 = выключен. Не применяется, когда задан порог
    трансляции или ручной остаток: их оператор задал явно."""
    parsed = _as_int(min_threshold)
    if parsed is None:
        return _row_response(request, db, uid_1c,
                             error="Порог кабинета: введите целое число (без запятой).",
                             error_field=f"threshold:{account_id}")
    min_threshold = max(0, parsed)
    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()
    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)
    setting.min_threshold = min_threshold
    log_action(db, user.username, "threshold_changed", f"{uid_1c} / кабинет #{account_id} -> {min_threshold}")
    db.commit()
    return _row_response(request, db, uid_1c)


# --------------------------------------------------------------------------- массовые действия

@router.get("/products/recalc-progress", response_class=HTMLResponse)
def recalc_progress(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Фрагмент прогресса — его страница опрашивает, пока расчёт идёт."""
    job = active_job(db) or last_job(db)
    return templates.TemplateResponse(request, "products_recalc.html", {
        "request": request, "recalc_job": job, "recalc_running": active_job(db) is not None,
        # Сюда попадают только опросом: значит страница открыта давно, и строки
        # таблицы под карточкой показывают состояние ДО расчёта.
        "polled": True,
    })


@router.post("/products/bulk")
def bulk_edit(
    request: Request, action: str = Form(...), uids: list[str] = Form(default=[]),
    int_value: str = Form(""), date_value: str = Form(""), account_id: str = Form(""),
    q: str = Form(""),
    only_proposals: bool = Form(False), only_blocked: bool = Form(False),
    hide_size_u: bool = Form(False), only_unfinished: bool = Form(False),
    only_on_platform: bool = Form(False), only_marked: bool = Form(False),
    hide_zero_stock: bool = Form(False), only_broadcasting: bool = Form(False),
    all_filtered: bool = Form(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая правка отмеченных строк: set_reserve | set_offset | clear_offset |
    broadcast_on | broadcast_off | set_active_since | set_base_date | set_fact |
    fact_from_stock.

    `all_filtered` — применить КО ВСЕМ строкам, подходящим под текущий фильтр, а
    не только к отмеченным галочками. Без этого режима «отметить все» на странице
    означало бы «первые 300 из 4812 подходящих», и оператор, отобрав фильтром
    нужное и нажав «отметить все», тихо обработал бы малую часть — а решил бы,
    что обработал всё. На каталоге в 152 тысячи SKU это неизбежно."""
    back = lambda: _back(q, only_proposals, only_blocked, hide_size_u, only_unfinished,
                         only_on_platform, only_marked, hide_zero_stock,
                         only_broadcasting)

    if not uids and not all_filtered:
        set_flash(request, "Не выбрано ни одной строки.", "warn")
        return back()

    n = d = None
    if action in ("set_reserve", "set_offset", "set_fact"):
        try:
            n = int((int_value or "").strip())
        except ValueError:
            set_flash(request, "Введите число для массовой правки.", "warn")
            return back()
        if action in ("set_reserve", "set_fact"):
            n = max(0, n)          # бронь и факт отрицательными не бывают, порог — бывает
    if action in ("set_active_since", "set_base_date", "stock_to_fact"):
        try:
            d = _parse_date(date_value)
        except ValueError:
            set_flash(request, "Дата должна быть в формате ГГГГ-ММ-ДД.", "warn")
            return back()
        if action in ("set_base_date", "stock_to_fact") and d is not None and d > today_local():
            set_flash(request, "Остатков на будущую дату в 1С нет.", "warn")
            return back()
        if action == "set_base_date" and d is None:
            # Пустое поле — это «дату не ввели», а не «снять расчёт». Молча снять
            # её стоило бы дороже всего остального на этой странице: `set_base_date`
            # вместе с датой стирает и ФАКТ — число, которое человек получил,
            # пересчитав склад руками, — и восстановить его нечем, а тут это
            # происходит сразу по всему отбору (до BULK_LIMIT строк). Для чисел
            # отказ при пустом поле уже стоял выше; дата была единственной
            # дверью, где пустота проходила молча. Снять расчёт массово можно
            # кнопкой «Сбросить порог» — она для этого и есть.
            set_flash(request, "Введите дату. Чтобы снять расчёт, нажмите "
                               "«Сбросить порог».", "warn")
            return back()

    if all_filtered:
        query = _base_query(db, q, only_proposals, only_blocked, hide_size_u,
                            only_unfinished, only_on_platform, only_marked,
                            hide_zero_stock, only_broadcasting)
        # `only_blocked` — фильтр не SQL-ный: точный расчёт «уходит 0» идёт по
        # лестнице приоритетов уже в Python, и массово применять правку по
        # приблизительному отбору нельзя. Отправляем оператора отметить строки.
        if only_blocked:
            set_flash(request, "С фильтром «только те, где уходит 0» массовая правка по "
                               "всему отбору не делается — точный список считается построчно. "
                               "Отметьте нужные строки галочками.", "warn")
            return back()
        matched = query.count()
        if matched > BULK_LIMIT:
            set_flash(request, f"Под фильтр попало {matched} строк — это больше предела "
                               f"в {BULK_LIMIT}. Уточните поиск или фильтр и повторите: "
                               f"правка такого объёма за один раз надолго заняла бы базу.",
                      "warn")
            return back()
        products = query.options(joinedload(Product.sync_settings)).all()
    else:
        products = db.query(Product).options(joinedload(Product.sync_settings)) \
            .filter(Product.uid_1c.in_(uids)).all()

    # Снимок на дату читаем ОДИН раз на всю пачку, а не по товару: иначе
    # простановка даты трёмстам отмеченным строкам — это шестьсот запросов.
    lookup = None
    if action in ("set_base_date", "stock_to_fact") and d is not None:
        lookup = stock_lookup(db, d)
        # Заявка на выгрузку — ОДНА на всю пачку, а не по товару: дата у всех
        # одна, и вторая заявка на неё всё равно не создаётся. Внутри цикла это
        # были бы лишние два запроса на каждую строку.
        ensure_snapshot_requested(db, d, user.username)

    if action == "recalc":
        # Не правка строк, а задание воркеру: по каждому товару надо опросить
        # каждый его кабинет по историческим заказам. В запросе это минуты.
        running = active_job(db)
        if running is not None:
            set_flash(request, f"Расчёт уже идёт (задание #{running.id}): обработано "
                               f"{running.processed} из {running.total}. Дождитесь конца — "
                               f"два задания шли бы по одним товарам и дублировали бы "
                               f"обращения к площадкам.", "warn")
            return back()
        ready = [p for p in products if p.offset_base_date is not None]
        if not ready:
            set_flash(request, "Ни у одного из выбранных товаров не задана дата расчёта. "
                               "Сначала «Записать остаток ЦС на дату».", "warn")
            return back()
        job = create_job(db, ready, user.username)
        log_action(db, user.username, "recalc_started",
                   f"задание #{job.id}, товаров {len(ready)}")
        db.commit()
        message = f"Расчёт запущен: {len(ready)} товаров. Прогресс виден на этой странице."
        if len(ready) < len(products):
            message += (f" Пропущено {len(products) - len(ready)} — у них не задана "
                        f"дата расчёта.")
        set_flash(request, message, "good")
        return back()

    target_account = None
    if action in ("cabinet_on", "cabinet_off"):
        try:
            target_account = db.query(PlatformAccount).filter(
                PlatformAccount.id == int(account_id)).first()
        except (TypeError, ValueError):
            target_account = None
        if target_account is None:
            set_flash(request, "Выберите кабинет в списке рядом с кнопкой.", "warn")
            return back()

    changed = skipped = refused = 0
    for p in products:
        if action in ("cabinet_on", "cabinet_off"):
            # Отметка кабинета сразу по всему отбору. Правила ровно те же, что у
            # галочки в строке (`toggle_sync`), и списаны с неё не случайно: там
            # включение ставит доотправку в очередь, а снятие — отзыв, и только
            # если на этот кабинет реально что-то уходило. Разойтись этим двум
            # путям нельзя, иначе массовое снятие обнулит живые карточки.
            if target_account is None:
                continue
            setting = next((x for x in p.sync_settings
                            if x.account_id == target_account.id), None)
            if setting is None:
                setting = SyncSetting(uid_1c=p.uid_1c, account_id=target_account.id)
                db.add(setting)
            want = action == "cabinet_on"
            if setting.enabled == want:
                continue                      # уже так — не действие
            if want:
                setting.enabled = True
                setting.enabled_at = now_utc()
                setting.has_proposal = False
                if p.broadcast_active_since is None:
                    p.broadcast_active_since = today_local()
                enqueue_full_resend(db, p.uid_1c, target_account.id)
            else:
                if should_withdraw(db, p, target_account.id):
                    enqueue_withdrawal(db, p.uid_1c, target_account.id)
                setting.enabled = False
            changed += 1
            continue

        if action == "broadcast_on" and _blocks_broadcast_on(p) is not None:
            # Молча пропустить нельзя: оператор решил бы, что включил всё
            # отобранное. Считаем и называем в отчёте.
            refused += 1
            continue
        if action == "set_reserve":
            p.reserve = n
            # Бронь входит в формулу порога — `recompute_offset` обязателен, как
            # и в одиночной правке строки. Без него новая бронь оставалась
            # словами: в пороге продолжала сидеть старая, и наружу уходило
            # больше, чем оператор только что оставил в продаже. Массовой
            # кнопкой — сразу по всему отбору.
            recompute_offset(p)
        elif action == "set_offset":
            p.broadcast_offset = n
            p.transmit_override = None
        elif action == "clear_offset":
            p.broadcast_offset = None
            p.offset_base_date = None       # см. set_offset: сброс снимает расчёт целиком
            p.offset_base_stock = None
            p.fact_at_date = None
        elif action == "broadcast_on":
            p.broadcast_enabled = True
            p.broadcast_requested_at = None
        elif action == "broadcast_off":
            # Отзыв — ровно как у галочки в строке (`toggle_broadcast`). Без него
            # на площадке оставалось последнее отправленное число, и она
            # продолжала по нему продавать, а заказы по этой паре живой опрос
            # уже принимает (гейт там по `SyncSetting.enabled`, которого
            # выключение трансляции не трогает): остаток у нас падает, у
            # площадки — нет. Это оверселл, и массовой кнопкой сразу по всему
            # отбору. `ever_transmitted`, а не `should_withdraw`: трансляцию мы
            # выключаем этой же строкой, и `should_withdraw` ответил бы «нечего»
            # по всем кабинетам.
            for setting in p.sync_settings:
                if setting.enabled and ever_transmitted(db, p.uid_1c, setting.account_id):
                    enqueue_withdrawal(db, p.uid_1c, setting.account_id,
                                       reason="broadcast_off")
            p.broadcast_enabled = False
            p.broadcast_requested_at = None    # см. toggle_broadcast
        elif action == "set_active_since":
            p.broadcast_active_since = d
        elif action == "set_base_date":
            # Снимок на дату уже есть — порог посчитается сразу; нет — строка
            # встанет в ожидание и доделается при приёме файла от 1С.
            set_base_date(db, p, d, lookup=lookup)
        elif action == "set_fact":
            p.fact_at_date = n
            recompute_offset(p)
        elif action == "recalc":
            pass          # обрабатывается до цикла: это задание, а не правка строк
        elif action == "stock_to_fact":
            # «Записать остаток ЦС на дату» — весь расчёт одним нажатием для
            # самого частого случая: цифра 1С верна, порог надо просто закрепить
            # на эту дату. Если в форме указана дата — ставим её и подтягиваем
            # остаток; иначе берём уже заданную у товара.
            if d is not None:
                set_base_date(db, p, d, lookup=lookup)
            if p.offset_base_stock is None:
                # Ответа 1С ещё нет — записывать нечего. Не ошибка: строка
                # досчитается сама, когда придёт файл. Но в отчёте это надо
                # назвать, иначе оператор решит, что обработаны все.
                skipped += 1
                continue
            p.fact_at_date = p.offset_base_stock
            recompute_offset(p)
        elif action == "fact_from_stock":
            # «Факт = остаток ЦС» — только там, где оператор ничего не вводил:
            # затирать введённые руками цифры массовой кнопкой нельзя.
            if p.fact_at_date is None and p.offset_base_stock is not None:
                p.fact_at_date = p.offset_base_stock
                recompute_offset(p)
        else:
            set_flash(request, "Неизвестное действие.", "warn")
            return back()
        if action not in ("set_active_since", "cabinet_on", "cabinet_off"):
            # У действий с кабинетом своя отправка — по тому кабинету, которого
            # они касаются. Общая доотправка добавила бы к ней записи по всем
            # остальным, ничего не изменив по сути.
            _repropagate(db, p, reason="bulk_edit")
        changed += 1

    scope = "по отбору" if all_filtered else "по отмеченным"
    if target_account is not None:
        scope += f", кабинет «{target_account.name}»"
    log_action(db, user.username, "products_bulk", f"{action} {scope} x{changed}")
    db.commit()
    message = f"Массовая правка ({scope}): изменено строк — {changed}."
    if skipped:
        message += (f" Пропущено {skipped}: 1С ещё не прислала выгрузку на эту дату — "
                    f"порог у них посчитается сам, когда придёт ответ.")
    if refused:
        message += (f" Не включено {refused}: расчёт по ним не закончен. Трансляция "
                    f"включается только после него — иначе на площадки уйдёт остаток, "
                    f"не сверенный с их продажами.")
    set_flash(request, message, "good" if not (skipped or refused) else "warn")
    return back()


@router.post("/products/dispatch-toggle")
def dispatch_toggle(
    request: Request, scope: str = Form(...), enabled: bool = Form(...), q: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Пауза рассылки на кабинеты: по площадке или по всем сразу. Пауза не трогает
    настройки товаров — очередь копится и уйдёт при включении."""
    query = db.query(PlatformAccount).filter(PlatformAccount.is_active.is_(True))
    if scope != "all":
        try:
            query = query.filter(PlatformAccount.platform == Platform(scope))
        except ValueError:
            set_flash(request, "Неизвестная площадка.", "warn")
            return _back(q)

    accounts = query.all()
    for a in accounts:
        a.dispatch_enabled = enabled
    log_action(db, user.username, "dispatch_toggle", f"scope={scope} enabled={enabled} x{len(accounts)}")
    db.commit()
    label = "всех площадок" if scope == "all" else scope.upper()
    state = "включена" if enabled else "выключена (пауза)"
    set_flash(request, f"Рассылка {label}: {state} — кабинетов затронуто {len(accounts)}.", "good")
    return _back(q)


# --------------------------------------------------------------------------- Excel

def _broadcast_cell(product: Product) -> str:
    """Что стоит в колонке «Трансляция» выгрузки.

    «Да» не только у включённой, но и у той, что ЖДЁТ расчёта: файл попросил
    включить, гейт согласился и отложил (`broadcast_requested_at`), и включит её
    `apply_pending_broadcast`, как только расчёт закончится.

    Иначе выгрузка сама убивала эти просьбы. Круг «выгрузил → поправил другую
    колонку → залил обратно» возвращал в ячейке «Нет», импорт читал его как
    осознанное выключение и просьбу снимал — ту самую, про которую минуту назад
    пообещал оператору «ждут расчёта и включатся сами: N». Ни в сообщении, ни в
    ошибках об этом не было ни слова: трансляция не включалась никогда, товар
    молча не продавался, а человек считал, что настроил всё файлом.

    Правило импорта при этом осталось прежним и нетронутым: «Нет» в файле
    по-прежнему снимает и просьбу тоже. Просто теперь «Нет» в этой ячейке
    означает правку ЧЕЛОВЕКА, а не наше собственное эхо.

    Текущее состояние из файла при этом не теряется: сколько уходит на площадки
    прямо сейчас, показывает соседняя колонка «Уходит на площадки» — у ждущей
    строки там ноль.
    """
    return "Да" if (product.broadcast_enabled
                    or product.broadcast_requested_at is not None) else "Нет"


def _cards_by_uid(db: Session) -> dict[str, list[str]]:
    """uid товара → названия кабинетов, в каталоге которых его баркод нашёлся.

    Подсказка оператору: файл говорит, где карточка ЕСТЬ, — значит видно, какой
    кабинет можно отметить, а какой отметить нельзя, потому что отправлять туда
    будет не по чему. Без неё это выяснялось поштучно на «Мэппинге», а массовую
    правку тем и делают, что поштучно долго.

    Кабинеты берём ВСЕ, включая выключенные, — как и страница «Есть на складе —
    нет на площадке». Каталог остаётся от кабинета, погашенного предохранителем
    или выключенного руками, и карточка на площадке от этого никуда не делась;
    привязка к активности заставляла бы подсказку моргать вместе с состоянием
    нашей связи, хотя вопрос она отвечает про товар.

    ОДИН запрос на всю выгрузку, а не запрос на товар. Ведущая таблица —
    `platform_catalog_items` (на бою 16 127 строк), баркод в `barcodes`
    проиндексирован. Запрос на строку при пределе выгрузки в 50 000 означал бы
    пятьдесят тысяч обращений к базе на одну кнопку — ровно тот анти-паттерн,
    который уже стоил нам неотвечающей страницы товаров.
    """
    names = {a.id: _account_label(a) for a in db.query(PlatformAccount).all()}
    found: dict[str, set[int]] = {}
    rows = (db.query(Barcode.uid_1c, PlatformCatalogItem.account_id)
            # `select_from` обязателен: без него SQLAlchemy берёт ведущей первую
            # сущность списка (Barcode) и пытается присоединить её саму к себе.
            .select_from(PlatformCatalogItem)
            .join(Barcode, Barcode.barcode == PlatformCatalogItem.barcode)
            .distinct()
            .all())
    for uid_1c, account_id in rows:
        if account_id in names:
            found.setdefault(uid_1c, set()).add(account_id)
    return {uid: sorted(names[a] for a in ids) for uid, ids in found.items()}


@router.get("/products/export")
def products_export(
    q: str = Query(""), only_proposals: bool = Query(False), only_blocked: bool = Query(False),
    hide_size_u: bool = Query(False), only_unfinished: bool = Query(False),
    only_on_platform: bool = Query(False), only_marked: bool = Query(False),
    hide_zero_stock: bool = Query(False), only_broadcasting: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Выгрузка отдаёт РОВНО ТО, что отобрано фильтрами на странице.

    Фильтр `only_unfinished` здесь отсутствовал: ссылка его передавала, а
    эндпоинт не принимал, и FastAPI молча его отбрасывал — оператор отбирал
    незавершённые строки, выгружал и получал весь каталог."""
    accounts = _active_accounts(db)
    products, total = _load_products(db, q, only_proposals, only_blocked, accounts,
                                     limit=EXPORT_LIMIT, hide_size_u=hide_size_u,
                                     only_unfinished=only_unfinished,
                                     only_on_platform=only_on_platform,
                                     only_marked=only_marked,
                                     hide_zero_stock=hide_zero_stock,
                                     only_broadcasting=only_broadcasting)
    total = abs(total)          # для файла знак «счёт оборван» роли не играет

    # Порядок колонок расчёта — тот же, что в строке на странице: дата, что
    # показала 1С, резерв, факт, и уже из них порог. Оператор правит файл,
    # сверяясь глазами со страницей, и разный порядок стоил бы ему ошибок.
    headers = ["ID_1С", "Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС",
               "Дата расчёта", "Остаток ЦС на дату", "Резерв", "Факт на дату",
               "Порог трансляции", "Трансляция", "Уходит на площадки",
               "Карточка есть в кабинетах"]
    for account in accounts:
        headers.append(f"{_account_label(account)} — Синхронизировать")
        headers.append(f"{_account_label(account)} — Порог")

    cards = _cards_by_uid(db)
    data = []
    for product in products:
        settings_map = {s.account_id: s for s in product.sync_settings}
        row = [product.uid_1c, product.article, product.size, product.color, product.name,
               product.stock_on_hand,
               product.offset_base_date.isoformat() if product.offset_base_date else "",
               product.offset_base_stock if product.offset_base_stock is not None else "",
               product.reserve,
               product.fact_at_date if product.fact_at_date is not None else "",
               product.broadcast_offset if product.broadcast_offset is not None else "",
               _broadcast_cell(product),
               explain(product, None, None).quantity,
               ", ".join(cards.get(product.uid_1c, []))]
        for account in accounts:
            setting = settings_map.get(account.id)
            row.append("Да" if setting and setting.enabled else "Нет")
            row.append(setting.min_threshold if setting else 0)
        data.append(row)

    if total > len(data):
        # Молчаливо обрезанная выгрузка — худший вариант: оператор правит её в Excel
        # и импортирует обратно, считая, что охватил весь каталог.
        data.append([f"⚠ показаны первые {len(data)} строк из {total} — уточните поиск или фильтр"])

    # Да/Нет — выбором из списка, а не набором руками. Опечатку в этих двух
    # колонках импорт читает как «Нет» и молча выключает то, что оператор
    # включал: пустая ячейка, «+», «да» с лишним пробелом дают один и тот же
    # результат, и увидеть его можно только по числу «изменено строк».
    choices = {"Трансляция": YES_NO}
    for account in accounts:
        choices[f"{_account_label(account)} — Синхронизировать"] = YES_NO
    return build_xlsx_response(headers, data, "товары_и_остатки.xlsx", choices=choices)


@router.post("/products/import")
def products_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая правка по отредактированному файлу экспорта. Ключ — колонка ID_1С.
    Заголовки колонок кабинетов менять нельзя: по ним определяется кабинет.

    Справочные колонки, которые импорт НЕ читает:
    «Уходит на площадки» — итог расчёта;
    «Карточка есть в кабинетах» — подсказка, где карточка товара существует;
    правится она не файлом, а заведением карточки на площадке;
    «Остаток ЦС на дату» — приходит из 1С, руками не задаётся;
    «Порог трансляции» — у товара с датой расчёта он выводится из даты, факта и
    брони. Принимать его ещё и из файла значило бы завести второй источник
    правды: залитый порог и посчитанный разошлись бы, и никто не сказал бы,
    какой верный. Менять порог надо через «Факт на дату». Если в файле порог всё
    же изменён, строка попадёт в ошибки — молча проигнорировать правку нельзя,
    оператор решил бы, что она применилась. У товара БЕЗ даты расчёта колонка
    работает по-прежнему: там порог живёт как введённое руками число."""
    accounts = _active_accounts(db)
    label_to_account = {_account_label(a): a for a in accounts}
    try:
        rows = read_xlsx_rows(read_upload(file.file))
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/products", status_code=303)

    updated, unchanged, errors = 0, 0, []
    deferred = 0                # строк, которые включатся сами после расчёта
    # По uid, а не списком: один товар может встретиться в файле дважды, и
    # задание получило бы по нему две одинаковые строки.
    needs_recalc: dict[str, Product] = {}
    # Настройки кабинетов, ЗАВЕДЁННЫЕ этим файлом. Сессия живёт с
    # `autoflush=False`: второй строке файла с тем же ID_1С запрос ниже первый
    # `db.add` НЕ ПОКАЖЕТ, и на ту же пару добавился бы второй объект под
    # `uq_product_account`. Коммит на весь импорт один, обработчика исключений
    # нет — оператор получал 500 и откат ВСЕГО файла: ни даты расчёта, ни факта,
    # ни брони, ни отметок кабинетов, по всем двадцати тысячам строк. А повтор
    # ID_1С в файле — обычное дело: склеили две выгрузки, скопировали строку,
    # чтобы поправить опечатку. Та же механика с тем же `autoflush=False` уже
    # закрыта в импорте «Мэппинга» и в загрузке каталога.
    settings_in_file: dict[tuple[str, int], SyncSetting] = {}
    # Кэш поиска остатка по датам, встретившимся в файле. Обычно дата одна на
    # весь файл, но полагаться на это нельзя. Без кэша импорт пятидесяти тысяч
    # строк — это сто тысяч запросов к базе.
    lookups: dict = {}

    for i, row in enumerate(rows, start=2):
        uid_1c = str(row.get("ID_1С") or "").strip()
        if not uid_1c:
            continue
        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            errors.append(f"строка {i}: товар с ID {uid_1c} не найден")
            continue

        touched = False
        date_changed = False    # дату задали этим файлом — значит дальше расчёт
        # Порог, КАКИМ ОН СТОЯЛ В ВЫГРУЗКЕ, до всех правок этой строки. Нужен,
        # чтобы отличить «оператор изменил порог» от «оператор изменил факт, а
        # порог оставил как был». Сравнивать с пересчитанным нельзя: он считается
        # из только что применённых даты, брони и факта, и правка факта —
        # единственный разрешённый способ сдвинуть порог — гарантированно с ним
        # разойдётся. То есть ошибку получала КАЖДАЯ строка, где человек сделал
        # ровно то, что велит подсказка на странице.
        offset_as_exported = product.broadcast_offset

        if "Резерв" in row:
            intent, text = _cell_intent(row.get("Резерв"))
            try:
                # Пусто — не трогаем; «-» — бронь ноль (её снятие и есть ноль).
                desired = None if intent == "skip" else (
                    0 if intent == "clear" else max(0, int(float(text))))
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный резерв")
                desired = None
            if desired is not None and product.reserve != desired:
                product.reserve = desired
                touched = True

        if "Дата расчёта" in row:
            cell = row.get("Дата расчёта")
            intent, text = _cell_intent(cell)
            skip_date = intent == "skip"      # пусто — дату не трогаем вовсе
            if isinstance(cell, datetime):
                desired_day = cell.date()
            elif isinstance(cell, date):
                desired_day = cell
            elif intent == "clear":
                # Явное «-»: снять расчёт. Вместе с датой уйдёт и факт — так же,
                # как при снятии даты на странице.
                desired_day = None
            elif skip_date:
                desired_day = product.offset_base_date
            else:
                try:
                    desired_day = _parse_date(text[:10])
                except ValueError:
                    errors.append(f"строка {i}: дата расчёта — формат ГГГГ-ММ-ДД")
                    desired_day = product.offset_base_date
            if skip_date:
                pass
            elif desired_day is not None and desired_day > today_local():
                errors.append(f"строка {i}: остатков на будущую дату в 1С нет")
            elif desired_day != product.offset_base_date:
                if desired_day is not None and desired_day not in lookups:
                    lookups[desired_day] = stock_lookup(db, desired_day)
                    # Заявка на эту дату — один раз, вместе с построением кэша.
                    ensure_snapshot_requested(db, desired_day, user.username)
                set_base_date(db, product, desired_day,
                              lookup=lookups.get(desired_day))
                touched = True
                date_changed = True

        if "Факт на дату" in row and _cell_intent(row.get("Факт на дату"))[0] != "skip":
            # Проверка на «skip» стоит ДО разбора числа, а не внутри него, и это
            # не перестановка ради красоты. Пустую ячейку `_cell_intent` отдаёт
            # как ("skip", ""), а `float("")` бросает ValueError — и пустая
            # ячейка давала ОШИБКУ ИМПОРТА, хотя не просила изменить ничего.
            # Выгрузка пишет сюда пустоту у каждой ненастроенной строки, так что
            # штатный круг «выгрузка → правка → импорт» на файле в двадцать тысяч
            # строк выдавал до сорока тысяч ложных ошибок (вторая — от соседнего
            # «Порога трансляции» с тем же дефектом). Показываются первые пять, и
            # всё это режется по `MAX_FLASH_CHARS`, поэтому НАСТОЯЩИЕ ошибки —
            # «трансляцию включить нельзя, нужен факт», «остатков на будущую дату
            # в 1С нет» — в сообщение не попадали никогда. Оператор видел жёлтую
            # плашку на успешном импорте и переставал её читать.
            intent, text = _cell_intent(row.get("Факт на дату"))
            try:
                desired_fact = None if intent == "clear" else max(0, int(float(text)))
            except ValueError:
                errors.append(f"строка {i}: некорректный факт на дату")
            else:
                # Пустая ячейка факт НЕ стирает: он получен пересчётом склада
                # руками, восстановить его нечем, а файл применяет правку сразу
                # ко всем строкам. Снять — явным «-». Сюда пустота уже не
                # доходит, её отсекает условие блока.
                if product.fact_at_date != desired_fact:
                    product.fact_at_date = desired_fact
                    touched = True

        if "Порог трансляции" in row and _cell_intent(row.get("Порог трансляции"))[0] != "skip":
            # То же самое, что и у «Факта на дату» выше: пусто — не трогаем, и
            # спросить об этом надо ДО разбора числа, иначе пустая ячейка даёт
            # ложную ошибку. Стереть порог пустотой нельзя и подавно: это вернуло
            # бы на площадки ПОЛНЫЙ остаток, по всему файлу и молча.
            intent, text = _cell_intent(row.get("Порог трансляции"))
            try:
                desired_offset = None if intent == "clear" else int(float(text))
            except ValueError:
                errors.append(f"строка {i}: некорректный порог трансляции")
            else:
                if product.offset_base_date is not None:
                    # Порог у такого товара расчётный, и файлом его не задают.
                    # Ругаемся, только если человек ИЗМЕНИЛ колонку: сверяем с
                    # тем, что стояло в выгрузке, а не с пересчитанным.
                    if desired_offset != offset_as_exported:
                        errors.append(
                            f"строка {i}: порог считается из даты и факта — "
                            f"правьте «Факт на дату», а не «Порог трансляции»")
                elif product.broadcast_offset != desired_offset:
                    product.broadcast_offset = desired_offset
                    if desired_offset is not None:
                        product.transmit_override = None
                    touched = True

        # Кабинеты разбираются РАНЬШЕ «Трансляции» намеренно. Гейт включения
        # спрашивает, есть ли отмеченный кабинет, покрытый расчётом; разбери мы
        # их после, в одном файле нельзя было бы и отметить кабинет, и включить
        # трансляцию — самый обычный сценарий массовой настройки.
        # Какие кабинеты окажутся отмеченными ПОСЛЕ применения файла. Считаем
        # сами, а не через product.sync_settings: сессия живёт с autoflush=False,
        # и только что добавленная настройка в коллекции объекта не появится —
        # гейт включения увидел бы товар без единого кабинета и отказал.
        enabled_after = enabled_account_ids(product)

        for label, account in label_to_account.items():
            sync_col = f"{label} — Синхронизировать"
            threshold_col = f"{label} — Порог"
            if sync_col not in row and threshold_col not in row:
                continue

            setting = settings_in_file.get((uid_1c, account.id))
            if setting is None:
                setting = db.query(SyncSetting).filter(
                    SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account.id,
                ).first()
            current_enabled = setting.enabled if setting else False
            current_threshold = setting.min_threshold if setting else 0

            # Пустая ячейка — «не менять». Прочитать её как «Нет» значило бы
            # снять галочку И ОТОЗВАТЬ остаток, то есть отправить ноль на живую
            # карточку площадки — по всему файлу разом.
            sync_intent, sync_text = _cell_intent(row.get(sync_col))
            if sync_col not in row or sync_intent == "skip":
                desired_enabled = current_enabled
            elif sync_intent == "clear":
                desired_enabled = False        # «-» — осознанное снятие
            else:
                desired_enabled = parse_bool_ru(sync_text)

            th_intent, th_text = _cell_intent(row.get(threshold_col))
            try:
                if threshold_col not in row or th_intent == "skip":
                    desired_threshold = current_threshold
                elif th_intent == "clear":
                    desired_threshold = 0      # снять порог кабинета и есть ноль
                else:
                    desired_threshold = int(float(th_text))
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный порог в колонке «{threshold_col}»")
                continue

            if desired_enabled == current_enabled and desired_threshold == current_threshold:
                continue

            if setting is None:
                setting = SyncSetting(uid_1c=uid_1c, account_id=account.id)
                db.add(setting)
                settings_in_file[(uid_1c, account.id)] = setting
            if desired_enabled and not current_enabled:
                setting.enabled_at = now_utc()
                setting.has_proposal = False
                enqueue_full_resend(db, uid_1c, account.id)
            elif (current_enabled and not desired_enabled
                    and should_withdraw(db, product, account.id)):
                # Снятие галочки через Excel — тот же осознанный отзыв, что и
                # галочкой в интерфейсе: без него на площадке остаётся последнее
                # отправленное число и она продолжает продавать. Если на кабинет
                # остаток не уходил, отзывать нечего.
                enqueue_withdrawal(db, uid_1c, account.id)
            setting.enabled = desired_enabled
            setting.min_threshold = max(0, desired_threshold)
            enabled_after.add(account.id) if desired_enabled else enabled_after.discard(account.id)
            touched = True

        # Пустая ячейка «Трансляции» главный выключатель товара не трогает:
        # «Нет» здесь не просто выключает, а ОТЗЫВАЕТ остаток со всех его
        # отмеченных кабинетов — ноль на каждую живую карточку.
        br_intent, br_text = _cell_intent(row.get("Трансляция")) if "Трансляция" in row else ("skip", "")
        if "Трансляция" in row and br_intent != "skip":
            desired_broadcast = (False if br_intent == "clear"
                                 else parse_bool_ru(br_text))
            # Тот же гейт, что и в интерфейсе: сначала расчёт, потом трансляция.
            # Без него Excel оставался обходным путём — строку, которой страница
            # включать не даёт, можно было включить файлом, и на площадки уехал
            # бы остаток, не сверенный с их продажами. Причём сразу пачкой.
            # Выключение не ограничено ничем и никогда.
            code, label = _calc_status(product, bool(enabled_after), enabled_after)
            blocked = None if (not desired_broadcast or code == "ready") else label
            if blocked and not product.broadcast_enabled:
                # «Ждём 1С» рассосётся само — но рассосётся оно в «нужен факт»,
                # если факта нет ни у товара, ни в этом файле. А «нужен факт»
                # сам не пройдёт: цифру вводит человек. Просьба, запомненная
                # здесь, повисла бы навсегда и молча — оператор ведь считает,
                # что всё указал файлом. Поэтому спрашиваем не только код
                # состояния, но и будет ли кому его закрыть.
                will_stall = code == "waiting" and product.fact_at_date is None
                if code in DEFERRABLE_CALC_STATUSES and not will_stall:
                    # Не ошибка, а ПРОСЬБА. Оператор одним файлом задаёт дату,
                    # факт, кабинеты и трансляцию — так он и думает о работе.
                    # Расчёт после этого идёт минутами и заканчивается уже без
                    # него, поэтому включать приходилось вторым заходом, разыскав
                    # те же строки в каталоге на сто пятьдесят тысяч позиций.
                    # Гейт при этом не ослаблен ни на грамм: включит строку
                    # `broadcast_gate.apply_pending_broadcast` ровно тогда, когда
                    # её включила бы и страница.
                    if product.broadcast_requested_at is None:
                        product.broadcast_requested_at = now_utc()
                        touched = True
                    deferred += 1
                elif will_stall:
                    errors.append(
                        f"строка {i}: трансляцию включить нельзя — нужен «Факт на "
                        f"дату». Дата задана, 1С ответит через несколько минут, но "
                        f"факт вводите вы: без него строка остановится на «нужен "
                        f"факт» и сама не включится.")
                else:
                    # Само не рассосётся: без кабинета заказы спрашивать негде,
                    # без даты расчёт не с чего начать, факт вводит человек.
                    errors.append(f"строка {i}: трансляцию включить нельзя — {blocked}")
            elif product.broadcast_enabled != desired_broadcast:
                if not desired_broadcast:
                    # Выключение файлом — тот же осознанный отзыв, что и галочкой
                    # в строке: без него на площадке остаётся последнее
                    # отправленное число, она продолжает продавать, а заказы по
                    # этой паре мы уже принимаем — остаток падает только у нас.
                    # Снятие галочки КАБИНЕТА в этом же импорте отзыв делает
                    # (см. выше), а выключение трансляции — нет; расхождение
                    # ничем не объяснялось.
                    for setting in product.sync_settings:
                        if setting.enabled and ever_transmitted(db, uid_1c, setting.account_id):
                            enqueue_withdrawal(db, uid_1c, setting.account_id,
                                               reason="broadcast_off")
                product.broadcast_enabled = desired_broadcast
                touched = True
            if not desired_broadcast and product.broadcast_requested_at is not None:
                # «Нет» в файле снимает и просьбу тоже: иначе трансляция
                # вернулась бы сама после ближайшего расчёта — молча и вопреки
                # тому, что оператор только что написал в файле.
                #
                # Работает это ровно потому, что ВЫГРУЗКА пишет сюда «Да» у
                # строки с непогашенной просьбой (см. `products_export`): файл
                # круговым путём возвращает то же, что отдал, и «Нет» в ячейке
                # означает правку человека, а не наше собственное эхо.
                product.broadcast_requested_at = None
                touched = True

        # Расчёт — следующий шаг ровно для тех строк, которым его и не хватает.
        # Запускаем его сами по двум поводам: дата задана этим файлом, или файл
        # попросил включить трансляцию. Оператор запускал его руками и по
        # памяти: в файле тысячи строк, отбор на странице к этому моменту уже
        # другой, и найти в каталоге ровно те же строки нечем.
        wants = product.broadcast_requested_at is not None
        if ((date_changed or wants) and product.offset_base_date is not None
                and enabled_after
                and _calc_status(product, True, enabled_after)[0]
                in DEFERRABLE_CALC_STATUSES):
            needs_recalc[product.uid_1c] = product

        if touched:
            # Один пересчёт на строку, уже после того как применены и дата, и
            # факт, и бронь: считать после каждой по отдельности значило бы
            # считать по половине данных.
            recompute_offset(product)
            updated += 1
            _repropagate(db, product, reason="excel_import")
        else:
            unchanged += 1

    recalc_note = ""
    if needs_recalc:
        running = active_job(db)
        if running is not None:
            recalc_note = (f" Расчёт НЕ запущен: уже идёт задание #{running.id} "
                           f"({running.processed} из {running.total}). Запустите его "
                           f"по этим строкам сами, когда оно закончится.")
        else:
            job = create_job(db, list(needs_recalc.values()), user.username)
            log_action(db, user.username, "recalc_started",
                       f"задание #{job.id} из импорта Excel, товаров {len(needs_recalc)}")
            recalc_note = f" Запущен расчёт: {len(needs_recalc)} товаров (задание #{job.id})."

    log_action(db, user.username, "products_bulk_import_excel", f"updated={updated}")
    db.commit()

    message = f"Изменено строк: {updated}. Без изменений: {unchanged}."
    if deferred:
        message += (f" Ждут расчёта и включатся сами: {deferred} — трансляцию по ним "
                    f"откроет не файл, а расчёт, когда остаток будет сверен с "
                    f"продажами площадок.")
    message += recalc_note
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        set_flash(request, f"{message} Ошибок: {len(errors)} ({shown}{more}).", "warn")
    else:
        set_flash(request, message, "good")
    return RedirectResponse("/products", status_code=303)
