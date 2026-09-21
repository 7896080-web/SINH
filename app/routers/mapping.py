from fastapi import APIRouter, Request, Depends, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_

from app.database import get_db
from app.templating import templates as shared_templates
from app.dependencies import get_current_user
from app.models import Barcode, Product, MappingConflict, Platform, PlatformAccount, PlatformCatalogItem, User
from app.excel_utils import build_xlsx_response, read_xlsx_rows, read_upload, format_dt, ExcelReadError
from app.flash import set_flash, pop_flash
from app.audit import log_action
from app.workers.client_factory import build_client
from app.workers.catalog_sync import load_platform_catalog
from app.routers.platform_matching import clear_clusters_cache
from app.workers.credentials import CredentialsMissing

router = APIRouter()
templates = shared_templates

# Сколько строк показывает страница. Столько же уходило и в выгрузку — молча:
# файл назывался «мэппинг баркодов», а содержал триста последних из ста
# пятидесяти четырёх тысяч, и нигде об этом не говорилось. Оператор открывает
# этот файл, чтобы переподвязать баркод, и «моей строки здесь нет» он понимает
# как «баркода нет в системе».
PAGE_LIMIT = 300
# Сколько уходит в выгрузку. Файл правят руками и загружают обратно, поэтому он
# обязан содержать то, что оператор отобрал, а не первый экран. Потолок —
# защита от попытки собрать весь справочник одним файлом.
EXPORT_LIMIT = 50000


def _mapped_query(db: Session, q: str, source_platform: str):
    query = db.query(Barcode).options(joinedload(Barcode.product))

    if q:
        like = f"%{q}%"
        query = query.join(Product).filter(
            or_(Barcode.barcode.ilike(like), Product.article.ilike(like), Product.name.ilike(like))
        )

    if source_platform:
        query = query.filter(Barcode.source_platform == source_platform)

    return query.order_by(Barcode.created_at.desc())


def _query_mapped(db: Session, q: str, source_platform: str, limit: int = PAGE_LIMIT):
    return _mapped_query(db, q, source_platform).limit(limit).all()


def _count_mapped(db: Session, q: str, source_platform: str) -> int:
    return _mapped_query(db, q, source_platform).order_by(None) \
        .enable_eagerloads(False).count()


def _count_conflicts(db: Session, q: str, account_id: str) -> int:
    """Сколько конфликтов под отбором ВСЕГО. Отдельным запросом, а не длиной
    показанного списка: страница показывает первые `PAGE_LIMIT`, и «300» вместо
    «300 из 2412» читается как «столько их и есть». Каждая строка здесь —
    случившаяся непроведённая продажа, и знать их число оператору важнее, чем
    видеть первые триста."""
    query = db.query(MappingConflict.id)
    if q:
        like = f"%{q.strip()}%"
        query = query.outerjoin(
            PlatformCatalogItem,
            and_(PlatformCatalogItem.barcode == MappingConflict.barcode,
                 PlatformCatalogItem.account_id == MappingConflict.account_id),
        ).filter(or_(
            MappingConflict.barcode.ilike(like),
            PlatformCatalogItem.name.ilike(like),
            PlatformCatalogItem.article.ilike(like),
        ))
    if account_id:
        query = query.filter(MappingConflict.account_id == int(account_id))
    return query.count()


def _query_conflicts(db: Session, q: str, account_id: str, limit: int = PAGE_LIMIT):
    query = (
        db.query(MappingConflict, PlatformCatalogItem, PlatformAccount)
        .join(PlatformAccount, PlatformAccount.id == MappingConflict.account_id)
        .outerjoin(
            PlatformCatalogItem,
            (PlatformCatalogItem.barcode == MappingConflict.barcode)
            & (PlatformCatalogItem.account_id == MappingConflict.account_id),
        )
    )

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            MappingConflict.barcode.ilike(like),
            PlatformCatalogItem.name.ilike(like),
            PlatformCatalogItem.article.ilike(like),
        ))

    if account_id:
        query = query.filter(MappingConflict.account_id == int(account_id))

    triples = query.order_by(MappingConflict.last_seen.desc()).limit(limit).all()
    # Разворачиваем в удобные для шаблона объекты — конфликт + кабинет +
    # опциональная спецификация с площадки (если была загружена кнопкой)
    result = []
    for conflict, catalog_item, account in triples:
        result.append({
            "barcode": conflict.barcode, "account_id": conflict.account_id,
            "account_name": account.name, "account_platform": account.platform,
            "attempts": conflict.attempts, "first_seen": conflict.first_seen,
            "last_seen": conflict.last_seen,
            "platform_name": catalog_item.name if catalog_item else None,
            "platform_article": catalog_item.article if catalog_item else None,
        })
    return result


def _render(request: Request, db: Session, user: User, view: str, q: str,
            source_platform: str, account_id: str, template: str):
    if view == "conflicts":
        rows = _query_conflicts(db, q, account_id)
        total = _count_conflicts(db, q, account_id)
    else:
        rows = _query_mapped(db, q, source_platform)
        # Считаем ВСЕГДА, а не только когда строк ровно триста: страница обязана
        # сказать, сколько их под отбором, иначе «моей строки нет» читается как
        # «баркода нет в системе».
        total = _count_mapped(db, q, source_platform)

    conflicts_count = db.query(MappingConflict).count()
    accounts = db.query(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name).all()

    return templates.TemplateResponse(request, template, {
        "request": request, "current_user": user, "active_page": "mapping",
        "rows": rows, "view": view, "q": q, "source_platform": source_platform, "account_id": account_id,
        "platforms": list(Platform), "accounts": accounts, "conflicts_count": conflicts_count,
        "total": total, "page_limit": PAGE_LIMIT, "export_limit": EXPORT_LIMIT,
        "flash": pop_flash(request) if template == "mapping.html" else None,
    })


@router.get("/mapping", response_class=HTMLResponse)
def mapping_page(
    request: Request, view: str = Query("mapped"), q: str = Query(""),
    source_platform: str = Query(""), account_id: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    return _render(request, db, user, view, q, source_platform, account_id, "mapping.html")


@router.get("/mapping/rows", response_class=HTMLResponse)
def mapping_rows(
    request: Request, view: str = Query("mapped"), q: str = Query(""),
    source_platform: str = Query(""), account_id: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """HTMX-фрагмент — только таблица, для живой фильтрации без перезагрузки страницы."""
    return _render(request, db, user, view, q, source_platform, account_id, "mapping_rows.html")


@router.get("/mapping/export")
def mapping_export(
    view: str = Query("mapped"), q: str = Query(""),
    source_platform: str = Query(""), account_id: str = Query(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if view == "conflicts":
        # Выгрузка берёт ВЕСЬ отбор, а не показанную страницу. Конфликт
        # сопоставления — это уже случившаяся непроведённая продажа: заказ
        # пришёл, разнести его не на что, и пока баркод не сопоставлен, каждый
        # следующий повторит то же самое. Файл для того и выгружают, чтобы
        # разобрать их пачкой; отдавать в нём первые триста и молчать об этом
        # значит оставить остальные неразобранными — продажи по ним идут мимо
        # нас, а остаток завышен ровно на них.
        rows = _query_conflicts(db, q, account_id, limit=EXPORT_LIMIT)
        headers = ["ID_1С", "Баркод", "Кабинет", "Площадка", "Название на площадке", "Артикул на площадке",
                   "Попыток", "Впервые увиден", "Последний раз"]
        data = [
            [None, r["barcode"], r["account_name"], r["account_platform"].value,
             r["platform_name"] or "", r["platform_article"] or "",
             r["attempts"], format_dt(r["first_seen"]), format_dt(r["last_seen"])]
            for r in rows
        ]
        filename = "конфликты_сопоставления.xlsx"
    else:
        rows = _query_mapped(db, q, source_platform, limit=EXPORT_LIMIT)
        # Размер и цвет — не украшение: файл этой выгрузки правят руками, чтобы
        # переподвязать баркод, и без них строки различаются только двумя
        # непрозрачными идентификаторами. Перепутать размеры в таком файле проще,
        # чем исправить то, ради чего его открыли.
        headers = ["ID_1С", "Баркод", "Артикул", "Наименование", "Размер", "Цвет",
                   "Источник", "Добавлен"]
        data = [
            [r.uid_1c, r.barcode, r.product.article, r.product.name,
             r.product.size, r.product.color,
             r.source_platform or "выгрузка из 1С", format_dt(r.created_at)]
            for r in rows
        ]
        filename = "мэппинг_баркодов.xlsx"

    return build_xlsx_response(headers, data, filename)


@router.post("/mapping/import")
def mapping_import(
    request: Request, file: UploadFile = File(...), repoint: bool = Form(False),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Добавляет новые баркоды из выгруженного и отредактированного файла.
    Колонка ID_1С — ключ сопоставления, это внутренний идентификатор товара,
    а не артикул.

    `repoint` — разрешить ПЕРЕПОДВЯЗКУ: баркод уже есть, но в файле указан
    другой товар. По умолчанию выключено, и такая строка остаётся ошибкой.

    Зачем это вообще нужно. В карточках 1С встречается перепутанный баркод:
    номер размера M на самом деле принадлежит L и наоборот. В 1С это
    исправляют, а до нас исправление не доезжает никак — приём справочника
    (`reconciliation.import_barcode_dict`) заводит только ОТСУТСТВУЮЩИЕ
    баркоды, а этот импорт до сих пор отвечал «уже привязан к другому товару»
    и строку пропускал. Другого способа переставить привязку в приложении нет,
    а пока она неверна, заказ на один размер списывает другой.

    Почему под галочкой, а не всегда. Переподвязка меняет, НА КАКОЙ товар
    спишется заказ по этому баркоду. Случайно загруженный старый файл молча
    переразнёс бы весь каталог, и продажи пошли бы с чужих размеров — такое
    действие должно быть заявлено явно.
    """

    try:
        rows = read_xlsx_rows(read_upload(file.file))
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/mapping", status_code=303)

    added, already_mapped, errors = 0, 0, []
    resolved_conflicts = 0
    repointed = 0
    conflicting = 0          # строки, где нужна переподвязка, а её не разрешили
    # Баркоды, ПРИВЯЗАННЫЕ ЭТОЙ ЖЕ загрузкой. Поиск существующей привязки идёт
    # запросом, а сессия живёт с `autoflush=False`: только что добавленный
    # баркод запрос не видит, и второй такой же в файле уходил вторым `INSERT`.
    # На коммите это `UNIQUE constraint failed: barcodes.barcode` — то есть 500
    # и потеря ВСЕГО импорта, включая переподвязки, сделанные выше по файлу.
    # А повтор в файле — вещь обычная: выгрузка с площадки, где один баркод у
    # нескольких строк, или просто склеенный из двух файл.
    in_file: dict[str, str] = {}

    for i, row in enumerate(rows, start=2):  # +2: строка 1 — заголовок, Excel считает с 1
        uid_1c = str(row.get("ID_1С") or "").strip()
        barcode = str(row.get("Баркод") or "").strip()

        if not barcode:
            continue  # пустая строка баркода — просто пропускаем, не ошибка
        if not uid_1c:
            errors.append(f"строка {i}: не заполнен ID_1С")
            continue

        product = db.query(Product).filter(Product.uid_1c == uid_1c).first()
        if product is None:
            errors.append(f"строка {i}: товар с ID {uid_1c} не найден")
            continue

        seen_uid = in_file.get(barcode)
        if seen_uid is not None:
            if seen_uid == uid_1c:
                already_mapped += 1          # та же строка дважды — не ошибка
            else:
                # Файл сам себе противоречит: один баркод на два товара. Молча
                # взять любой значило бы решить за человека, на какой товар
                # спишется заказ.
                errors.append(f"строка {i}: баркод {barcode} в этом же файле уже "
                              f"привязан к товару {seen_uid} — оставьте одну строку")
            continue

        existing = db.query(Barcode).filter(Barcode.barcode == barcode).first()
        if existing is not None:
            if existing.uid_1c == uid_1c:
                already_mapped += 1
                continue
            if not repoint:
                conflicting += 1
                errors.append(f"строка {i}: баркод {barcode} привязан к другому товару "
                              f"({existing.uid_1c}) — нужна галочка «разрешить переподвязку»")
                continue

            was_uid = existing.uid_1c
            existing.uid_1c = uid_1c
            existing.source_platform = "excel_repoint"
            # Отметку «актуализирован» снимаем у ОБОИХ товаров. Расчёт поднимает
            # заказы площадок по баркодам товара (`recalc.collect_orders`), и оба
            # набора только что изменились: прежний вывод сделан по чужим
            # баркодам и к новому составу не относится. Оставить отметку значило
            # бы разрешить трансляцию остатка, сверенного не по тем продажам.
            for affected_uid in (was_uid, uid_1c):
                p = db.query(Product).filter(Product.uid_1c == affected_uid).first()
                if p is not None:
                    p.recalc_done_at = None
                    # Пустая СТРОКА, а не NULL: покрытие аннулировано, но
                    # отслеживаем мы его по-прежнему. NULL означал бы «расчёта
                    # никогда не было», и ступень 2 лестницы перестала бы
                    # срабатывать вовсе — переподвязка ОТКРЫВАЛА бы трансляцию
                    # несверенного остатка вместо того, чтобы закрыть её.
                    p.recalc_account_ids = ""
            log_action(db, user.username, "barcode_repointed",
                       f"{barcode}: {was_uid} -> {uid_1c}")
            in_file[barcode] = uid_1c
            repointed += 1
            db.query(MappingConflict).filter(MappingConflict.barcode == barcode).delete()
            continue

        db.add(Barcode(barcode=barcode, uid_1c=uid_1c, source_platform="excel_import"))
        in_file[barcode] = uid_1c
        added += 1

        # Если этот баркод раньше висел в конфликтах сопоставления (в любом
        # кабинете) — конфликт только что разрешён руками, запись не нужна.
        deleted = db.query(MappingConflict).filter(MappingConflict.barcode == barcode).delete()
        resolved_conflicts += deleted

    # Массовый импорт меняет ключ сопоставления для многих товаров сразу —
    # соседние операции пишутся в журнал, эта не писалась.
    log_action(db, user.username, "mapping_import",
               f"добавлено {added}, уже были {already_mapped}, переподвязано {repointed}, "
               f"разрешено конфликтов {resolved_conflicts}, ошибок {len(errors)}")
    db.commit()

    message = f"Добавлено баркодов: {added}. Уже были сопоставлены: {already_mapped}."
    if repointed:
        message += (f" Переподвязано: {repointed} — по этим товарам снята отметка "
                    f"«актуализирован», нужен новый расчёт.")
    if conflicting and not repoint:
        message += (f" Требуют переподвязки: {conflicting}. Включите галочку "
                    f"«разрешить переподвязку» и загрузите файл ещё раз.")
    if resolved_conflicts:
        message += f" Разрешено конфликтов сопоставления: {resolved_conflicts}."
    if errors:
        shown = "; ".join(errors[:5])
        more = f" и ещё {len(errors) - 5}" if len(errors) > 5 else ""
        message += f" Ошибок: {len(errors)} ({shown}{more})."
        set_flash(request, message, "warn")
    else:
        set_flash(request, message, "good")

    return RedirectResponse("/mapping", status_code=303)


@router.post("/mapping/load-catalog/{account_id}")
def mapping_load_catalog(
    request: Request, account_id: int,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """Тянет полную спецификацию карточек кабинета (баркод, артикул,
    название) и сохраняет снимок — используется для обогащения списка
    конфликтов и для ручного сопоставления по баркоду."""

    account = db.query(PlatformAccount).filter(PlatformAccount.id == account_id).first()
    if account is None:
        set_flash(request, f"Кабинет #{account_id} не найден.", "warn")
        return RedirectResponse("/mapping?view=conflicts", status_code=303)

    try:
        client = build_client(db, account_id)
        stats = load_platform_catalog(db, client, account)
        # Картина кластеров на «Сопоставлении площадок» собрана по СТАРОМУ
        # каталогу — после загрузки она врёт. Минута ожидания тут была бы
        # особенно обидной: человек нажал «Загрузить каталог» ровно затем, чтобы
        # увидеть новое.
        clear_clusters_cache()
    except CredentialsMissing as e:
        set_flash(request, f"Не удалось загрузить спецификацию «{account.name}»: {e}", "warn")
        return RedirectResponse("/mapping?view=conflicts", status_code=303)
    except Exception as e:
        set_flash(request, f"Ошибка при загрузке спецификации «{account.name}»: {e}", "warn")
        return RedirectResponse("/mapping?view=conflicts", status_code=303)

    message = (
        f"{account.name}: загружено карточек {stats['fetched']}, "
        f"уже сопоставлено {stats['already_mapped']}, "
        f"новых для разбора {stats['new_conflicts']}."
    )
    if stats.get("no_barcode"):
        message += f" Без баркода на площадке: {stats['no_barcode']}."

    log_action(db, user.username, "catalog_loaded", f"{account.name}: {stats}")
    db.commit()
    set_flash(request, message, "good")

    return RedirectResponse("/mapping?view=conflicts", status_code=303)
