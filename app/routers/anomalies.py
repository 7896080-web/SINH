from datetime import datetime
from app.timeutils import now_utc

from fastapi import APIRouter, Request, Depends, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import case, func, or_

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import SyncAnomaly, SyncSetting, Product, PlatformAccount, AnomalyReason, AnomalyStatus, User
from app.anomalies import close_fixed_anomalies
from app.transmit import enqueue_full_resend
from app.excel_utils import build_xlsx_response, read_xlsx_rows, read_upload, parse_bool_ru, format_dt, ExcelReadError, MAX_IMPORT_ROWS
from app.flash import set_flash, pop_flash
from app.audit import log_action

router = APIRouter()
templates = shared_templates

REASON_LABELS = {
    # Порядок подписей важен не меньше порядка строк: человек читает их как
    # приговор. «Невключённый товар» — заказ, который живой опрос отверг по
    # гейту и НЕ ПРОВЁЛ; «пара без галочки» — продажа, которую суточная сверка
    # уже провела, но карточка продаёт дальше, и число на ней не обновляет никто.
    AnomalyReason.order_on_unmanaged: "Продажа по паре без галочки — карточку никто не обновляет",
    AnomalyReason.order_misrouted: "Баркод ведёт не на тот товар 1С — списали с соседа по карточке",
    AnomalyReason.order_on_disabled: "Заказ на невключённый товар",
    AnomalyReason.missing_barcode: "Нет баркода при включённой галочке",
}

REASON_PRIORITY = {
    # Пара без галочки — первой: по ней площадка продаёт прямо сейчас по числу,
    # которое никто не обновляет, то есть разбирают её раньше остальных, а
    # предел показа страницы иначе выкинул бы с экрана ровно эти строки.
    AnomalyReason.order_on_unmanaged: 0,
    # Подмена плательщика оверселл уже остановила, значит разбирают её после
    # пары без галочки, но раньше несопоставленного баркода: там продажа не
    # проведена вовсе, а здесь проведена и арифметика верна.
    AnomalyReason.order_misrouted: 1,
    AnomalyReason.order_on_disabled: 2,
    AnomalyReason.missing_barcode: 3,
}

# Сколько строк показывает страница. Предел нужен по той же причине, по которой
# он есть на «Товарах»: строки в статусе `new` не чистит никто намеренно
# (аномалия в `new` — незаконченное дело), а пока идёт переход со старой системы
# «заказ на невключённый товар» приходит десятками в сутки и сам не
# закрывается — пару товар+кабинет никто не включит, потому что этим каталогом
# мы ещё не управляем. Десять тысяч сгруппированных строк — это мегабайты
# разметки КАЖДЫЕ ДВЕ МИНУТЫ (страница обновляет себя сама) на веб-службе,
# которая в это же время принимает заказы с площадок.
PAGE_LIMIT = 300
# Сколько уходит в выгрузку. Файл правят руками и загружают обратно, значит он
# обязан содержать ОТБОР, а не первый экран: «моей строки в файле нет» оператор
# читает как «её нет в системе».
#
# Предел тот же, что у ИМПОРТА, и это не совпадение: файл тут ездит по кругу
# («выгрузил -> отметил -> залил обратно»). Поставь мы выгрузке потолок выше, и
# на отборе между двумя числами страница отдавала бы файл, который наша же
# страница потом не примет, — причём отказ пришёл бы уже после ручной правки
# нескольких тысяч строк.
EXPORT_LIMIT = MAX_IMPORT_ROWS


def _grouped_query(db: Session, status: str, q: str, account_id: str, reason: str):
    query = (
        db.query(
            SyncAnomaly.uid_1c, SyncAnomaly.account_id, SyncAnomaly.reason,
            func.count(SyncAnomaly.id).label("occurrences"),
            func.max(SyncAnomaly.detected_at).label("last_detected"),
            Product.article, Product.name, Product.size, Product.color,
            PlatformAccount.name.label("account_name"), PlatformAccount.platform.label("account_platform"),
        )
        .join(Product, Product.uid_1c == SyncAnomaly.uid_1c)
        .join(PlatformAccount, PlatformAccount.id == SyncAnomaly.account_id)
        .filter(SyncAnomaly.status == status)
        .filter(SyncAnomaly.is_test.is_(False))  # тестовые аномалии со страницы тестирования сюда не попадают
    )

    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.article.ilike(like), Product.name.ilike(like)))

    if account_id:
        query = query.filter(SyncAnomaly.account_id == int(account_id))

    if reason:
        query = query.filter(SyncAnomaly.reason == reason)

    return query.group_by(
        SyncAnomaly.uid_1c, SyncAnomaly.account_id, SyncAnomaly.reason,
        Product.article, Product.name, Product.size, Product.color,
        PlatformAccount.name, PlatformAccount.platform,
    )


def _load_grouped_anomalies(db: Session, status: str, q: str, account_id: str,
                            reason: str, limit: int | None = None):
    """Строки отбора, сгруппированные по паре товар+кабинет+причина.

    Сортировка в SQL, а не в Python, и это не вкусовщина: с пределом показа
    сортировка после `LIMIT` отобрала бы ПРОИЗВОЛЬНЫЕ триста групп и разложила
    бы по порядку уже их — то есть страница показывала бы не самые свежие
    аномалии, а случайные, ничем об этом не сказав. Порядок причин берётся из
    `REASON_PRIORITY`, чтобы он был ОДИН: по алфавиту значения встают наоборот,
    и повтори мы его здесь числами, два порядка однажды разошлись бы.
    """
    # Предел читается ИЗ МОДУЛЯ, а не из значения по умолчанию у параметра:
    # умолчание связывается в момент объявления функции, и подмена константы в
    # тесте на него не действует вовсе — то есть проверка предела проверяла бы
    # не предел. Ноль значит «без предела».
    limit = PAGE_LIMIT if limit is None else limit
    priority = case(*[(SyncAnomaly.reason == kind, rank)
                      for kind, rank in REASON_PRIORITY.items()], else_=99)
    query = _grouped_query(db, status, q, account_id, reason).order_by(
        priority, func.max(SyncAnomaly.detected_at).desc())
    return query.limit(limit).all() if limit else query.all()


def _count_grouped_anomalies(db: Session, status: str, q: str, account_id: str,
                             reason: str) -> int:
    """Сколько строк под отбором ВСЕГО — отдельным запросом, а не длиной списка.

    `len(rows)` показывал бы «300» при любом числе аномалий, и «300» без «из
    2412» читается как «столько их и есть». Тот же тройной дефект уже находили
    на выгрузке конфликтов сопоставления: предел, врущий счётчик и молчание о
    том, что список обрезан, — чинить надо все три, иначе оператор всё равно не
    узнает.
    """
    return _grouped_query(db, status, q, account_id, reason).count()


def _resolve_sync_and_anomalies(db: Session, uid_1c: str, account_id: int) -> bool:
    """Включает синхронизацию (если ещё не включена) и закрывает связанные
    аномалии по паре товар+кабинет. Используется и кнопкой в интерфейсе,
    и массовым импортом из Excel — логика ровно одна, не дублируется."""

    setting = db.query(SyncSetting).filter(
        SyncSetting.uid_1c == uid_1c, SyncSetting.account_id == account_id,
    ).first()

    if setting is None:
        setting = SyncSetting(uid_1c=uid_1c, account_id=account_id)
        db.add(setting)

    changed = not setting.enabled
    if changed:
        setting.enabled = True
        setting.enabled_at = now_utc()
        setting.has_proposal = False
        enqueue_full_resend(db, uid_1c, account_id)

    db.query(SyncAnomaly).filter(
        SyncAnomaly.uid_1c == uid_1c,
        SyncAnomaly.account_id == account_id,
        SyncAnomaly.status == AnomalyStatus.new,
    ).update({"status": AnomalyStatus.resolved})

    return changed


# Как часто страница перезапрашивает себя сама. Аномалии появляются при опросе
# заказов (раз в минуту-две) и исчезают по мере включения товаров — оператор
# держит страницу открытой и работает по ней, руками он её не перезагрузит.
REFRESH_SECONDS = 120


def _render(request: Request, db: Session, user: User, status: str, q: str, account_id: str, reason: str, template: str):
    # Причину аномалии устраняют не только кнопкой на этой странице: товар
    # включают на «Товарах», массовой правкой, импортом, баркод приезжает из 1С.
    # Без этого строки оставались в списке навсегда, и он не пустел от работы.
    if close_fixed_anomalies(db):
        db.commit()
    rows = _load_grouped_anomalies(db, status, q, account_id, reason)
    # Считаем отбор ТОЛЬКО когда список реально обрезан. Пришло меньше предела —
    # значит это и есть всё, и лишний `GROUP BY` на каждую загрузку и на каждое
    # автообновление (а оно каждые две минуты) был бы ровно той ценой, от
    # которой страницу тут и спасают.
    #
    # Цена названа честно: в ОБРЕЗАННОМ случае агрегация идёт дважды — `GROUP
    # BY` с сортировкой остановиться раньше не умеет, предел режет разметку и
    # работу браузера, а не стоимость запроса. Уйти от второго прохода можно
    # оконным `count(*) over ()` в том же запросе, но он требует SQLite не
    # старше 3.25, а версию на боевой машине мы не проверяли — её печатает
    # `scripts/probe_growth.py`. Выдумывать за платформу мы не станем, тем же
    # правилом, по которому у Ozon нет придуманного лимита запросов.
    total = (len(rows) if len(rows) < PAGE_LIMIT
             else _count_grouped_anomalies(db, status, q, account_id, reason))
    new_count = db.query(SyncAnomaly).filter(
        SyncAnomaly.status == AnomalyStatus.new.value, SyncAnomaly.is_test.is_(False),
    ).count()
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()

    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "anomalies",
        "rows": rows, "status": status, "q": q, "account_id": account_id, "reason": reason,
        "accounts": accounts, "reasons": list(AnomalyReason), "reason_labels": REASON_LABELS,
        "new_count": new_count,
        "total": total, "page_limit": PAGE_LIMIT, "export_limit": EXPORT_LIMIT,
        "refresh_seconds": REFRESH_SECONDS,
        "flash": pop_flash(request) if template == "anomalies.html" else None,
    })


@router.get("/anomalies", response_class=HTMLResponse)
def anomalies_page(
    request: Request, status: str = Query("new"), q: str = Query(""), account_id: str = Query(""),
    reason: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, status, q, account_id, reason, "anomalies.html")


@router.get("/anomalies/rows", response_class=HTMLResponse)
def anomalies_rows(
    request: Request, status: str = Query("new"), q: str = Query(""), account_id: str = Query(""),
    reason: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, status, q, account_id, reason, "anomalies_rows.html")


@router.post("/anomalies/{uid_1c}/{account_id}/resolve", response_class=HTMLResponse)
def resolve_anomaly(
    uid_1c: str, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    _resolve_sync_and_anomalies(db, uid_1c, account_id)
    log_action(db, user.username, "anomaly_resolved", f"{uid_1c} / кабинет #{account_id}")
    db.commit()

    return HTMLResponse("")


@router.get("/anomalies/export")
def anomalies_export(
    status: str = Query("new"), q: str = Query(""), account_id: str = Query(""), reason: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    # ВЕСЬ отбор, а не показанную страницу: по этому файлу разбирают пачкой, и
    # каждая строка здесь — уже случившаяся непроведённая продажа.
    rows = _load_grouped_anomalies(db, status, q, account_id, reason,
                                   limit=EXPORT_LIMIT)

    headers = ["ID_1С", "Артикул", "Наименование", "Кабинет", "Площадка", "Причина",
               "Заказов поймано", "Последний раз", "Синхронизировать"]
    data = [
        [r.uid_1c, r.article, r.name, r.account_name, r.account_platform.value, REASON_LABELS[r.reason],
         r.occurrences, format_dt(r.last_detected), "Нет"]
        for r in rows
    ]

    return build_xlsx_response(headers, data, "аномалии_синхронизации.xlsx")


@router.post("/anomalies/import")
def anomalies_import(
    request: Request, file: UploadFile = File(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Массовая разборка аномалий из отредактированного файла: строки, где в
    колонке «Синхронизировать» проставлено 'Да', обрабатываются. Колонки
    ID_1С и Кабинет — ключ сопоставления (по имени кабинета, регистр важен)."""

    accounts_by_name = {a.name: a for a in db.query(PlatformAccount).all()}
    try:
        rows = read_xlsx_rows(read_upload(file.file))
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/anomalies", status_code=303)

    resolved, skipped, errors = 0, 0, []

    for i, row in enumerate(rows, start=2):
        uid_1c = str(row.get("ID_1С") or "").strip()
        account_name = str(row.get("Кабинет") or "").strip()

        if not uid_1c or not account_name:
            continue

        if not parse_bool_ru(row.get("Синхронизировать")):
            skipped += 1
            continue

        account = accounts_by_name.get(account_name)
        if account is None:
            errors.append(f"строка {i}: кабинет «{account_name}» не найден")
            continue

        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            errors.append(f"строка {i}: товар с ID {uid_1c} не найден")
            continue

        _resolve_sync_and_anomalies(db, uid_1c, account.id)
        resolved += 1

    log_action(db, user.username, "anomalies_bulk_resolved_excel", f"resolved={resolved}")
    db.commit()

    message = f"Обработано (включено и закрыто): {resolved}. Пропущено (без отметки): {skipped}."
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        message += f" Ошибок: {len(errors)} ({shown}{more})."
        set_flash(request, message, "warn")
    else:
        set_flash(request, message, "good")

    return RedirectResponse("/anomalies", status_code=303)
