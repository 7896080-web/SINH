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
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Product, PlatformAccount, Platform, SyncSetting, User
from app.flash import set_flash, pop_flash
from app.audit import log_action
from app.timeutils import now_utc
from app.excel_utils import build_xlsx_response, read_xlsx_rows, parse_bool_ru, ExcelReadError
from app.transmit import (explain, sku_quantity, enqueue_full_resend, enqueue_withdrawal,
                          offset_from_base, recompute_offset)
from app.offset_base import (ensure_snapshot_requested, set_base_date, stock_at_date,
                              stock_lookup)
from app.recalc import active_job, create_job, last_job

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _calc_status(product: Product, has_cabinet: bool = True) -> tuple[str, str]:
    """Состояние расчёта строки: (код, подпись для оператора).

    Отвечает на единственный вопрос, который у оператора возникает на каталоге в
    152 тысячи SKU: «эту строку я уже обработал или нет?». По цифрам в ячейке это
    не понять — пустой факт выглядит одинаково и когда его не вводили, и когда
    решили, что учёт 1С верен.

    `ready` означает всё сразу: порог посчитан и подтверждён, отгрузки площадок
    за период проведены в 1С, остаток ЦС актуален — товар можно включать в
    трансляцию. Раньше этим словом назывался только посчитанный порог, но это
    разные состояния, и путать их нельзя: с непроведёнными отгрузками остаток
    завышен, и включённая трансляция отправит на площадки лишнее.
    """
    if not has_cabinet:
        # Ни одного отмеченного кабинета: заказы спрашивать негде и транслировать
        # некуда. Без этой подписи оператор видел «нужен расчёт», запускал его и
        # получал молчаливый пустой проход — задание отчитывалось «0 заказов», а
        # причина оставалась только в строке задания, которой на странице нет.
        return ("no_cabinet", "не выбран кабинет")
    if product.offset_base_date is None:
        return ("none", "расчёт не начат")
    if product.offset_base_stock is None:
        return ("waiting", "ждём 1С")
    if product.fact_at_date is None:
        # Порог уже считается (сводится к брони), но человек цифру не подтвердил.
        # Пока не подтвердил — строка не «обработана».
        return ("need_fact", "нужен факт")
    if product.recalc_done_at is None:
        # Порог посчитан, но отгрузки на маркетплейсы за период в 1С ещё не
        # проведены: остаток ЦС завышен, и включать трансляцию рано — уедет
        # число больше реального.
        return ("need_recalc", "нужен расчёт")
    return ("ready", "актуализирован")


def _row(product: Product, accounts: list[PlatformAccount],
         all_accounts: dict[int, PlatformAccount] | None = None) -> dict:
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
    has_cabinet = any(s.enabled for s in product.sync_settings)

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
        "calc_status": _calc_status(product, has_cabinet)[0],
        "calc_status_label": _calc_status(product, has_cabinet)[1],
        "transmit_override": product.transmit_override,   # legacy: только предупреждение
        "broadcast_enabled": product.broadcast_enabled,
        "active_since": product.broadcast_active_since,
        "has_barcode": len(product.barcodes) > 0,
        "sku_quantity": sku_quantity(product),
        "sku_blocked": sku.blocked,
        "sku_reason": sku.reason,
        "accounts": per_account,
    }


PAGE_LIMIT = 300          # без фильтров: «первые 300 из 152 тысяч по алфавиту» —
                          # витрина, работают всегда через отбор
# С ФИЛЬТРАМИ отдаём весь отбор. Потолок всё же нужен: строка тяжёлая (дата,
# факт, бронь плюс по несколько полей на каждый кабинет), и каталог целиком
# браузер не построит.
FILTERED_LIMIT = 5000
EXPORT_LIMIT = 50000      # потолок выгрузки: защита от попытки собрать .xlsx на весь каталог
# Потолок массовой правки «по всему фильтру». Каждая строка тянет за собой запись
# в очередь рассылки на каждый отмеченный кабинет, а база — SQLite, в которую в
# это же время пишет планировщик. Правка всего каталога разом заняла бы её
# минутами, и страница висела бы без признаков жизни.
BULK_LIMIT = 20000


def _base_query(db: Session, q: str, only_proposals: bool, only_blocked: bool,
                hide_size_u: bool = False, only_unfinished: bool = False):
    """Отбор в SQL — ДО ограничения по количеству строк.

    Раньше сначала брались первые 300 товаров по алфавиту, и лишь потом
    применялись фильтры: при каталоге в 152 тысячи SKU «Только с предложениями»
    и «Только те, где уходит 0» показывали пусто, потому что в первых 300 по
    алфавиту таких товаров не было."""
    query = db.query(Product).options(joinedload(Product.sync_settings), joinedload(Product.barcodes))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like)))
    if only_proposals:
        query = query.filter(Product.sync_settings.any(SyncSetting.has_proposal.is_(True)))
    if only_blocked:
        # Предварительный отбор: «ничего не уходит» имеет смысл только для товаров,
        # отмеченных хотя бы в одном кабинете. Точный расчёт — ниже, по лестнице.
        query = query.filter(Product.sync_settings.any(SyncSetting.enabled.is_(True)))
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
    return query.order_by(Product.name)


def _blocked_everywhere(product: Product, accounts: list[PlatformAccount]) -> bool:
    """Ни один отмеченный кабинет не получает положительное число."""
    settings_map = {s.account_id: s for s in product.sync_settings}
    marked = [a for a in accounts if (settings_map.get(a.id) and settings_map[a.id].enabled)]
    return bool(marked) and all(explain(product, settings_map.get(a.id), a).quantity == 0 for a in marked)


def _load_products(db: Session, q: str, only_proposals: bool, only_blocked: bool,
                   accounts: list[PlatformAccount], limit: int = PAGE_LIMIT,
                   hide_size_u: bool = False,
                   only_unfinished: bool = False) -> tuple[list[Product], int]:
    """Возвращает (строки, сколько всего подходит под фильтр). Второе число нужно,
    чтобы честно написать оператору «показано 300 из N», а не делать вид, что это всё.
    Отрицательное значение = счёт оборван на пределе сканирования, в интерфейсе
    показывается как «N+»."""
    query = _base_query(db, q, only_proposals, only_blocked, hide_size_u, only_unfinished)

    if not only_blocked:
        total = query.order_by(None).count()
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
            only_unfinished: bool = False):
    accounts = _active_accounts(db)
    all_accounts = {a.id: a for a in db.query(PlatformAccount).all()}
    # С фильтрами показываем ВЕСЬ отбор: оператор сузил список именно затем,
    # чтобы увидеть его целиком. Без фильтров — витрина в 300 строк: «первые 300
    # из 152 тысяч по алфавиту» всё равно ни о чём не говорят.
    filtered = bool(q or only_proposals or only_blocked or hide_size_u or only_unfinished)
    products, total = _load_products(db, q, only_proposals, only_blocked, accounts,
                                     limit=FILTERED_LIMIT if filtered else PAGE_LIMIT,
                                     hide_size_u=hide_size_u,
                                     only_unfinished=only_unfinished)
    rows = [_row(p, accounts, all_accounts) for p in products]
    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "products",
        "rows": rows, "total": total, "page_limit": PAGE_LIMIT,
        "q": q, "only_proposals": only_proposals, "only_blocked": only_blocked,
        "hide_size_u": hide_size_u,
        "only_unfinished": only_unfinished,
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
                  hide_size_u: bool = False, only_unfinished: bool = False) -> str:
    """Фильтры страницы в виде строки запроса."""
    from urllib.parse import urlencode

    params = {"q": q} if q else {}
    for name, on in (("only_proposals", only_proposals), ("only_blocked", only_blocked),
                     ("hide_size_u", hide_size_u), ("only_unfinished", only_unfinished)):
        if on:
            params[name] = "true"
    return urlencode(params)


def _back(q: str, only_proposals: bool = False, only_blocked: bool = False,
          hide_size_u: bool = False, only_unfinished: bool = False) -> RedirectResponse:
    """Назад на страницу С ТЕМИ ЖЕ ФИЛЬТРАМИ.

    Раньше возвращался только поиск: оператор отбирал строки фильтром «только
    незавершённый расчёт», применял массовую правку — и попадал на полный список,
    где отобранных строк уже не найти."""
    query = _filter_query(q, only_proposals, only_blocked, hide_size_u, only_unfinished)
    return RedirectResponse(f"/products{'?' + query if query else ''}", status_code=303)


# --------------------------------------------------------------------------- страница

@router.get("/products", response_class=HTMLResponse)
def products_page(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    only_blocked: bool = Query(False), hide_size_u: bool = Query(False),
    only_unfinished: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if not _active_accounts(db):
        set_flash(request, "Пока нет ни одного активного кабинета — добавьте его на странице «API-ключи».", "warn")
    return _render(request, db, user, q, only_proposals, only_blocked, hide_size_u,
                   "products.html", only_unfinished=only_unfinished)


@router.get("/products/rows", response_class=HTMLResponse)
def products_rows(
    request: Request, q: str = Query(""), only_proposals: bool = Query(False),
    only_blocked: bool = Query(False), hide_size_u: bool = Query(False),
    only_unfinished: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, q, only_proposals, only_blocked, hide_size_u,
                   "products_rows.html", only_unfinished=only_unfinished)


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
    if day is not None and day > now_utc().date():
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
        product.broadcast_enabled = enabled
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
                if setting.enabled:
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
    документа в 1С. Главный выключатель товара всегда вёл себя именно так."""
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
        enqueue_full_resend(db, uid_1c, account_id)
        log_action(db, user.username, "sync_enabled", f"{uid_1c} / кабинет #{account_id}")
    elif not enabled and was_enabled:
        enqueue_withdrawal(db, uid_1c, account_id)
        log_action(db, user.username, "sync_disabled", f"{uid_1c} / кабинет #{account_id} (в очередь 0)")
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
    int_value: str = Form(""), date_value: str = Form(""), q: str = Form(""),
    only_proposals: bool = Form(False), only_blocked: bool = Form(False),
    hide_size_u: bool = Form(False), only_unfinished: bool = Form(False),
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
    back = lambda: _back(q, only_proposals, only_blocked, hide_size_u, only_unfinished)

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
        if action in ("set_base_date", "stock_to_fact") and d is not None and d > now_utc().date():
            set_flash(request, "Остатков на будущую дату в 1С нет.", "warn")
            return back()

    if all_filtered:
        query = _base_query(db, q, only_proposals, only_blocked, hide_size_u, only_unfinished)
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

    changed = skipped = 0
    for p in products:
        if action == "set_reserve":
            p.reserve = n
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
        elif action == "broadcast_off":
            p.broadcast_enabled = False
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
        if action != "set_active_since":
            _repropagate(db, p, reason="bulk_edit")
        changed += 1

    scope = "по отбору" if all_filtered else "по отмеченным"
    log_action(db, user.username, "products_bulk", f"{action} {scope} x{changed}")
    db.commit()
    message = f"Массовая правка ({scope}): изменено строк — {changed}."
    if skipped:
        message += (f" Пропущено {skipped}: 1С ещё не прислала выгрузку на эту дату — "
                    f"порог у них посчитается сам, когда придёт ответ.")
    set_flash(request, message, "good" if not skipped else "warn")
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

@router.get("/products/export")
def products_export(
    q: str = Query(""), only_proposals: bool = Query(False), only_blocked: bool = Query(False),
    hide_size_u: bool = Query(False), only_unfinished: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Выгрузка отдаёт РОВНО ТО, что отобрано фильтрами на странице.

    Фильтр `only_unfinished` здесь отсутствовал: ссылка его передавала, а
    эндпоинт не принимал, и FastAPI молча его отбрасывал — оператор отбирал
    незавершённые строки, выгружал и получал весь каталог."""
    accounts = _active_accounts(db)
    products, total = _load_products(db, q, only_proposals, only_blocked, accounts,
                                     limit=EXPORT_LIMIT, hide_size_u=hide_size_u,
                                     only_unfinished=only_unfinished)
    total = abs(total)          # для файла знак «счёт оборван» роли не играет

    # Порядок колонок расчёта — тот же, что в строке на странице: дата, что
    # показала 1С, резерв, факт, и уже из них порог. Оператор правит файл,
    # сверяясь глазами со страницей, и разный порядок стоил бы ему ошибок.
    headers = ["ID_1С", "Артикул", "Размер", "Цвет", "Наименование", "Остаток ЦС",
               "Дата расчёта", "Остаток ЦС на дату", "Резерв", "Факт на дату",
               "Порог трансляции", "Трансляция", "Уходит на площадки"]
    for account in accounts:
        headers.append(f"{_account_label(account)} — Синхронизировать")
        headers.append(f"{_account_label(account)} — Порог")

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
               "Да" if product.broadcast_enabled else "Нет",
               explain(product, None, None).quantity]
        for account in accounts:
            setting = settings_map.get(account.id)
            row.append("Да" if setting and setting.enabled else "Нет")
            row.append(setting.min_threshold if setting else 0)
        data.append(row)

    if total > len(data):
        # Молчаливо обрезанная выгрузка — худший вариант: оператор правит её в Excel
        # и импортирует обратно, считая, что охватил весь каталог.
        data.append([f"⚠ показаны первые {len(data)} строк из {total} — уточните поиск или фильтр"])

    return build_xlsx_response(headers, data, "товары_и_остатки.xlsx")


@router.post("/products/import")
def products_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая правка по отредактированному файлу экспорта. Ключ — колонка ID_1С.
    Заголовки колонок кабинетов менять нельзя: по ним определяется кабинет.

    Справочные колонки, которые импорт НЕ читает:
    «Уходит на площадки» — итог расчёта;
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
        rows = read_xlsx_rows(file.file.read())
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/products", status_code=303)

    updated, unchanged, errors = 0, 0, []
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

        if "Резерв" in row:
            try:
                desired = max(0, int(row.get("Резерв") or 0))
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный резерв")
            else:
                if product.reserve != desired:
                    product.reserve = desired
                    touched = True

        if "Дата расчёта" in row:
            raw = row.get("Дата расчёта")
            raw = "" if raw is None else str(raw).strip()
            if isinstance(row.get("Дата расчёта"), datetime):
                desired_day = row["Дата расчёта"].date()
            elif isinstance(row.get("Дата расчёта"), date):
                desired_day = row["Дата расчёта"]
            else:
                try:
                    desired_day = _parse_date(raw[:10]) if raw else None
                except ValueError:
                    errors.append(f"строка {i}: дата расчёта — формат ГГГГ-ММ-ДД")
                    desired_day = product.offset_base_date
            if desired_day is not None and desired_day > now_utc().date():
                errors.append(f"строка {i}: остатков на будущую дату в 1С нет")
            elif desired_day != product.offset_base_date:
                if desired_day is not None and desired_day not in lookups:
                    lookups[desired_day] = stock_lookup(db, desired_day)
                    # Заявка на эту дату — один раз, вместе с построением кэша.
                    ensure_snapshot_requested(db, desired_day, user.username)
                set_base_date(db, product, desired_day,
                              lookup=lookups.get(desired_day))
                touched = True

        if "Факт на дату" in row:
            raw = row.get("Факт на дату")
            raw = "" if raw is None else str(raw).strip()
            try:
                desired_fact = max(0, int(float(raw))) if raw else None
            except ValueError:
                errors.append(f"строка {i}: некорректный факт на дату")
            else:
                if product.fact_at_date != desired_fact:
                    product.fact_at_date = desired_fact
                    touched = True

        if "Порог трансляции" in row:
            raw = row.get("Порог трансляции")
            raw = "" if raw is None else str(raw).strip()
            try:
                desired_offset = int(raw) if raw else None
            except ValueError:
                errors.append(f"строка {i}: некорректный порог трансляции")
            else:
                if product.offset_base_date is not None:
                    # Порог у такого товара расчётный. Сверяем с тем, что выйдет
                    # из даты и факта, и расхождение показываем ошибкой.
                    if desired_offset != offset_from_base(product):
                        errors.append(
                            f"строка {i}: порог считается из даты и факта — "
                            f"правьте «Факт на дату», а не «Порог трансляции»")
                elif product.broadcast_offset != desired_offset:
                    product.broadcast_offset = desired_offset
                    if desired_offset is not None:
                        product.transmit_override = None
                    touched = True

        if "Трансляция" in row:
            desired_broadcast = parse_bool_ru(row.get("Трансляция"))
            if product.broadcast_enabled != desired_broadcast:
                product.broadcast_enabled = desired_broadcast
                touched = True

        for label, account in label_to_account.items():
            sync_col = f"{label} — Синхронизировать"
            threshold_col = f"{label} — Порог"
            if sync_col not in row and threshold_col not in row:
                continue

            setting = db.query(SyncSetting).filter(
                SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account.id,
            ).first()
            current_enabled = setting.enabled if setting else False
            current_threshold = setting.min_threshold if setting else 0

            desired_enabled = parse_bool_ru(row.get(sync_col)) if sync_col in row else current_enabled
            try:
                desired_threshold = int(row.get(threshold_col) or 0) if threshold_col in row else current_threshold
            except (TypeError, ValueError):
                errors.append(f"строка {i}: некорректный порог в колонке «{threshold_col}»")
                continue

            if desired_enabled == current_enabled and desired_threshold == current_threshold:
                continue

            if setting is None:
                setting = SyncSetting(uid_1c=uid_1c, account_id=account.id)
                db.add(setting)
            if desired_enabled and not current_enabled:
                setting.enabled_at = now_utc()
                setting.has_proposal = False
                enqueue_full_resend(db, uid_1c, account.id)
            setting.enabled = desired_enabled
            setting.min_threshold = max(0, desired_threshold)
            touched = True

        if touched:
            # Один пересчёт на строку, уже после того как применены и дата, и
            # факт, и бронь: считать после каждой по отдельности значило бы
            # считать по половине данных.
            recompute_offset(product)
            updated += 1
            _repropagate(db, product, reason="excel_import")
        else:
            unchanged += 1

    log_action(db, user.username, "products_bulk_import_excel", f"updated={updated}")
    db.commit()

    message = f"Изменено строк: {updated}. Без изменений: {unchanged}."
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        set_flash(request, f"{message} Ошибок: {len(errors)} ({shown}{more}).", "warn")
    else:
        set_flash(request, message, "good")
    return RedirectResponse("/products", status_code=303)
