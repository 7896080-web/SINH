"""Возвраты: приёмка, разбор, список, печать наклейки.

Три экрана, а не одна таблица, потому что у них разные задачи и разный темп.
Приёмка — одно поле и сканер, кладовщик смотрит на экран краем глаза. Разбор —
одна вещь крупно и решение по ней. Список — то, что осталось доделать.

Площадка выбирается КОРОБКОЙ и живёт в сессии: кладовщик едет в конкретный ПВЗ и
привозит возвраты одной площадки, поэтому спрашивать её на каждый скан незачем —
это и медленнее, и ошибочнее. Полоса с названием и счётчиком висит на экране всё
время: забыть, под какой площадкой принимаешь, должно быть трудно.
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import returns as R
from app.audit import log_action
from app.barcode128 import svg as barcode_svg
from app.database import get_db
from app.dependencies import get_current_user
from app.flash import pop_flash, set_flash
from app.excel_utils import (ExcelReadError, MAX_IMPORT_ROWS, build_xlsx_response,
                             format_dt, read_upload, read_xlsx_rows)
from app.models import (FtpTask, Platform, Product, ReturnItem, ReturnItemLog,
                        ReturnStatus, ScrapReason)
from app.templating import templates
from app.timeutils import (local_date_of, local_day_start_utc, now_utc,
                           today_local)

router = APIRouter()

PLATFORM_LABELS = {Platform.wb: "Wildberries", Platform.ozon: "Ozon", Platform.kit: "Kit"}
# Цвет полосы приёмки. Различаются и тоном, и светлотой: одного тона мало —
# цвет тут работает как подпись, а её видят боковым зрением.
PLATFORM_COLORS = {Platform.wb: "#6b3fa0", Platform.ozon: "#2f5d9f", Platform.kit: "#b5750f"}

RECENT_ON_SCREEN = 12          # сколько принятых показываем под полем
LIST_LIMIT = 300               # потолок списка; список — рабочий, не архив


def _platform_of_session(request: Request) -> Platform:
    raw = request.session.get("returns_platform") or Platform.wb.value
    try:
        return Platform(raw)
    except ValueError:
        return Platform.wb


def _products(db: Session, items) -> dict:
    uids = {i.uid_1c for i in items if i.uid_1c}
    if not uids:
        return {}
    return {p.uid_1c: p for p in db.query(Product).filter(Product.uid_1c.in_(uids)).all()}


def _base(request: Request, user, db: Session) -> dict:
    # Флеш достаём ЗДЕСЬ, один раз на весь раздел. Без этого `base.html` рисует
    # пустоту, а `set_flash` из обработчиков пропадает молча: человек нажимает
    # «Вернуть в продажу», отказ уходит в никуда, экран не меняется — и это
    # ровно тот немой отказ, с которым весь проект и воюет.
    return {"request": request, "current_user": user, "active_page": "returns",
            "flash": pop_flash(request),
            "labels": R.RETURN_LABELS, "scrap_labels": R.SCRAP_LABELS,
            "hints": R.RETURN_HINTS, "scrap_operations": R.SCRAP_OPERATION,
            # Режим спрашивается на КАЖДОЙ странице раздела, а не только на
            # приёмке: полоса обязана висеть всюду, где человек что-то решает.
            # Увидь он её один раз на входе, через час он про неё не вспомнит.
            "test_mode": R.test_mode(db),
            # Число в вопросе — не украшение: в тренировочном режиме мог быть
            # принят НАСТОЯЩИЙ возврат, и это единственный шанс заметить.
            "test_count": R.test_items_count(db),
            "platform_labels": PLATFORM_LABELS, "platform_colors": PLATFORM_COLORS,
            "label_number": R.label_number, "ReturnStatus": ReturnStatus}


# ------------------------------------------------------------------ приёмка

@router.get("/returns", response_class=HTMLResponse)
def acceptance(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    platform = _platform_of_session(request)
    # Список под полем — ЭТА коробка: полоса наверху утверждает площадку, и
    # показывать под ней чужие сканы значит утверждать неправду. Человек сверяет
    # по нему, что скан прошёл, — соседняя площадка в списке читается как «принял
    # не туда» и заставляет отменять то, что в порядке.
    recent = (db.query(ReturnItem)
              .filter(ReturnItem.status == ReturnStatus.accepted,
                      ReturnItem.platform == platform)
              .order_by(ReturnItem.created_at.desc()).limit(RECENT_ON_SCREEN).all())
    # «Сегодня» — МЕСТНОЕ, а не UTC-шное. Боевой сервер в Москве: с полуночи до
    # трёх часов UTC-шные сутки ещё вчерашние, и счётчик показывал бы вчерашнюю
    # коробку вместе с сегодняшней. Утренняя приёмка в эти часы как раз и идёт.
    box = (db.query(func.count(ReturnItem.id))
           .filter(ReturnItem.platform == platform,
                   ReturnItem.created_at >= local_day_start_utc(today_local())
                   ).scalar() or 0)
    ctx = _base(request, user, db)
    # Снимаем ОБА разовых состояния здесь, а не в шаблоне. Cookie сессии
    # записывается в заголовки ДО того, как отрисуется тело, — то есть
    # `session.pop` из шаблона до браузера не доезжает вовсе, и состояние
    # остаётся в сессии навсегда. Для наклейки это печать на КАЖДУЮ загрузку
    # страницы, а страница у кладовщика открыта весь день: одна наклейка
    # превращается в пачку, и на вещах оказываются чужие номера — ровно то,
    # ради чего наклейка и заводилась.
    ctx.update({"platform": platform, "recent": recent, "box_count": box,
                "products": _products(db, recent),
                "platforms": list(Platform),
                # Вопрос живёт до ОТВЕТА, а не до перезагрузки: снимай его
                # отрисовка, и F5 по привычке молча стёр бы решение по реальной
                # второй вещи — она не завелась бы, и никто бы об этом не узнал.
                # Снимают его обе кнопки, и они же единственные пути.
                "ask": request.session.get("returns_ask"),
                # А печать — РОВНО один раз: см. выше.
                "to_print": request.session.pop("returns_print", None)})
    return templates.TemplateResponse(request, "returns_acceptance.html", ctx)


@router.post("/returns/platform")
def set_platform(request: Request, platform: str = Form(...), user=Depends(get_current_user)):
    try:
        request.session["returns_platform"] = Platform(platform).value
    except ValueError:
        pass
    return RedirectResponse("/returns", status_code=303)


@router.post("/returns/scan")
def scan(request: Request, code: str = Form(""), confirm_second: str = Form(""),
         db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Одно поле на всё: товарный баркод заводит вещь, `RET-…` открывает её.

    Так снимается целый класс ошибок «отсканировал не в то поле» — человеку не
    надо выбирать поле и помнить режим.
    """
    code = (code or "").strip()
    number = R.parse_label(code)
    if number is not None:
        return RedirectResponse(f"/returns/item/{number}", status_code=303)

    platform = _platform_of_session(request)
    twin = R.recent_same_barcode(db, code, platform) if code else None
    if twin is not None and not confirm_second:
        if R.is_scanner_bounce(twin):
            # Дребезг сканера: то же самое за две секунды — точно один жест.
            # Спрашивать тут значит приучить жать «да» не глядя.
            return RedirectResponse("/returns", status_code=303)
        request.session["returns_ask"] = {"code": code, "twin": R.label_number(twin),
                                          "ago": int((now_utc() - twin.created_at).total_seconds() // 60)}
        return RedirectResponse("/returns", status_code=303)

    request.session.pop("returns_ask", None)
    try:
        item = R.accept(db, code, platform, is_test=R.test_mode(db))
    except R.ReturnError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/returns", status_code=303)
    log_action(db, user.username, "return_accepted",
               f"{R.label_number(item)} {item.barcode} {platform.value}")
    db.commit()
    # Печать — отдельным окном: страница приёмки остаётся с полем в фокусе,
    # иначе следующий скан уехал бы в никуда.
    request.session["returns_print"] = item.id
    return RedirectResponse("/returns", status_code=303)


@router.post("/returns/test-mode")
def switch_test_mode(request: Request, on: str = Form(""),
                     db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Включить тренировку — или выключить её, СТЕРЕВ всё, что она завела.

    Выход и уборка — одно действие намеренно. Разними их, и установка осталась бы
    с тренировочными вещами в боевом списке: отличить их можно только по пометке,
    а список для того и нужен, чтобы верить ему без разбора. Хуже того, такая
    вещь висела бы в «ждём 1С» вечно — настоящего ответа по ней не будет никогда.

    Число удаляемых называет подтверждение на странице, и это не украшение: в
    тренировочном режиме мог быть принят НАСТОЯЩИЙ возврат — человек ошибся
    режимом, — и число есть единственный шанс это заметить до того, как запись
    исчезнет.
    """
    turning_on = on == "1"
    removed = 0
    if not turning_on:
        removed = R.clear_test_data(db)
    R.set_test_mode(db, turning_on)
    log_action(db, user.username, "returns_test_mode",
               f"{'включён' if turning_on else 'выключен'}, стёрто вещей: {removed}")
    db.commit()
    if turning_on:
        set_flash(request, "Тренировочный режим включён. Наружу ничего не уходит, "
                           "а при выходе всё принятое здесь будет стёрто.", "warn")
    else:
        set_flash(request, f"Боевой режим. Тренировочных вещей стёрто: {removed}.",
                  "good")
    return RedirectResponse("/returns", status_code=303)


@router.post("/returns/skip-repeat")
def skip_repeat(request: Request, user=Depends(get_current_user)):
    """«Это повтор, не заводить». Ссылка на ту же страницу тут не годилась:
    вопрос живёт в сессии, и без явного снятия он возвращался бы на каждой
    загрузке — то есть кнопка выглядела бы нажатой впустую."""
    request.session.pop("returns_ask", None)
    return RedirectResponse("/returns", status_code=303)


@router.post("/returns/{item_id}/cancel")
def cancel(request: Request, item_id: int, db: Session = Depends(get_db),
           user=Depends(get_current_user)):
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return RedirectResponse("/returns", status_code=303)
    number = R.label_number(item)
    try:
        R.cancel_acceptance(db, item)
    except R.ReturnError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/returns", status_code=303)
    log_action(db, user.username, "return_cancelled", number)
    db.commit()
    set_flash(request, f"{number} — приёмка отменена. Наклейку выбросьте.", "info")
    return RedirectResponse("/returns", status_code=303)


# ------------------------------------------------------------------ разбор

@router.get("/returns/item/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    ctx = _base(request, user, db)
    if item is None:
        ctx.update({"item": None, "not_found": item_id})
        return templates.TemplateResponse(request, "returns_item.html", ctx)
    product = (db.query(Product).filter(Product.uid_1c == item.uid_1c).first()
               if item.uid_1c else None)
    events = (db.query(ReturnItemLog).filter(ReturnItemLog.return_id == item.id)
              .order_by(ReturnItemLog.at.asc()).all())
    ctx.update({"item": item, "product": product, "events": events, "not_found": None,
                "can": {s: R.can_change(item, s) for s in ReturnStatus},
                "scrap_reasons": list(ScrapReason)})
    return templates.TemplateResponse(request, "returns_item.html", ctx)


@router.post("/returns/item/{item_id}/status")
def set_status(request: Request, item_id: int, to: str = Form(...),
               scrap_reason: str = Form(""), note: str = Form(""),
               db: Session = Depends(get_db), user=Depends(get_current_user)):
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return RedirectResponse("/returns", status_code=303)
    try:
        target = ReturnStatus(to)
        reason = ScrapReason(scrap_reason) if scrap_reason else None
        R.change_status(db, item, target, note=note, scrap_reason=reason)
    except (ValueError, R.ReturnError) as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(f"/returns/item/{item_id}", status_code=303)
    log_action(db, user.username, "return_status",
               f"{R.label_number(item)} → {target.value} {scrap_reason}")
    db.commit()
    return RedirectResponse(f"/returns/item/{item_id}", status_code=303)


@router.post("/returns/item/{item_id}/to-sale")
def to_sale(request: Request, item_id: int, db: Session = Depends(get_db),
            user=Depends(get_current_user)):
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return RedirectResponse("/returns", status_code=303)
    try:
        R.send_to_1c(db, item)
    except R.ReturnError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(f"/returns/item/{item_id}", status_code=303)
    log_action(db, user.username, "return_to_sale", R.label_number(item))
    db.commit()
    return RedirectResponse(f"/returns/item/{item_id}", status_code=303)


@router.post("/returns/item/{item_id}/scrap")
def scrap(request: Request, item_id: int, scrap_reason: str = Form(""),
          db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Решение «утилизировать» — теперь это ОТПРАВКА В 1С, а не отметка.

    Вещь числится на складе площадки в 1С (туда её увезло перемещение при приёме
    заказа), поэтому списать её — два документа: вернуть на ЦС и списать с него.
    Поставь мы статус кнопкой, вещь была бы выброшена физически и вечно числилась
    бы на складе площадки по учёту.
    """
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return RedirectResponse("/returns", status_code=303)
    try:
        reason = ScrapReason(scrap_reason) if scrap_reason else None
        R.send_scrap_to_1c(db, item, reason)
    except (ValueError, R.ReturnError) as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(f"/returns/item/{item_id}", status_code=303)
    log_action(db, user.username, "return_scrapped",
               f"{R.label_number(item)} {scrap_reason}")
    db.commit()
    return RedirectResponse(f"/returns/item/{item_id}", status_code=303)


@router.post("/returns/item/{item_id}/recall")
def recall(request: Request, item_id: int, db: Session = Depends(get_db),
           user=Depends(get_current_user)):
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return RedirectResponse("/returns", status_code=303)
    try:
        R.recall_before_send(db, item)
    except R.ReturnError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(f"/returns/item/{item_id}", status_code=303)
    log_action(db, user.username, "return_recalled", R.label_number(item))
    db.commit()
    set_flash(request, "Отправка отменена, задание удалено до ухода в 1С.", "info")
    return RedirectResponse(f"/returns/item/{item_id}", status_code=303)


@router.post("/returns/item/{item_id}/simulate-1c")
def simulate(request: Request, item_id: int, ok: str = Form(""),
             db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Ответить за 1С — только по тренировочной вещи, проверка в домене."""
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return RedirectResponse("/returns", status_code=303)
    try:
        R.simulate_1c(db, item, ok == "1")
    except R.ReturnError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse(f"/returns/item/{item_id}", status_code=303)
    log_action(db, user.username, "returns_simulated_1c",
               f"{R.label_number(item)} → {'OK' if ok == '1' else 'ERROR'}")
    db.commit()
    return RedirectResponse(f"/returns/item/{item_id}", status_code=303)


# ------------------------------------------------------------------ список

@router.get("/returns/list", response_class=HTMLResponse)
def listing(request: Request, status: str = "", platform: str = "", q: str = "",
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(ReturnItem)
    if status:
        try:
            query = query.filter(ReturnItem.status == ReturnStatus(status))
        except ValueError:
            status = ""
    if platform:
        try:
            query = query.filter(ReturnItem.platform == Platform(platform))
        except ValueError:
            platform = ""
    if q.strip():
        number = R.parse_label(q)
        if number is not None:
            query = query.filter(ReturnItem.id == number)
        else:
            like = f"%{q.strip()}%"
            uids = [p.uid_1c for p in db.query(Product).filter(Product.article.ilike(like)).all()]
            query = query.filter((ReturnItem.barcode.ilike(like))
                                 | (ReturnItem.uid_1c.in_(uids) if uids else False))
    rows = query.order_by(ReturnItem.created_at.desc()).limit(LIST_LIMIT).all()

    # Сводка «площадка × статус» — один запрос, а не по клетке.
    grid = {}
    for pl, st, n in (db.query(ReturnItem.platform, ReturnItem.status,
                               func.count(ReturnItem.id))
                      .filter(ReturnItem.status.in_(R.IN_WORK))
                      .group_by(ReturnItem.platform, ReturnItem.status).all()):
        grid[(pl, st)] = n

    ctx = _base(request, user, db)
    ctx.update({"rows": rows, "products": _products(db, rows), "grid": grid,
                "in_work": R.IN_WORK, "platforms": list(Platform),
                "f_status": status, "f_platform": platform, "q": q,
                "total": query.order_by(None).count(), "limit": LIST_LIMIT,
                "now": now_utc()})
    return templates.TemplateResponse(request, "returns_list.html", ctx)


# ------------------------------------------------------------------ утилизация

# Что показывает страница утилизации. ОБА статуса, и это важно: «утилизирован»
# ставит только ответ 1С, а решение принято раньше — вещь уже выброшена
# физически. Покажи мы одни проведённые, список за сегодня был бы почти пуст и
# человек решил бы, что утилизация не работает.
SCRAP_VIEW = (ReturnStatus.awaiting_scrap, ReturnStatus.scrapped)

SCRAP_EXPORT_HEADERS = ["Номер", "Статус", "Площадка", "Причина", "Баркод",
                        "Артикул", "Размер", "Цвет", "Наименование",
                        "Принят", "Решение принято", "Документ 1С"]


def _scrap_query(db: Session, status: str, platform: str, reason: str, q: str):
    """Отбор страницы. ОДИН на список, выгрузку и счётчик — разойдись они,
    файл содержал бы не то, что человек видел на экране, и узнать об этом было
    бы неоткуда."""
    query = db.query(ReturnItem).filter(ReturnItem.status.in_(SCRAP_VIEW))
    if status:
        try:
            query = query.filter(ReturnItem.status == ReturnStatus(status))
        except ValueError:
            pass
    if platform:
        try:
            query = query.filter(ReturnItem.platform == Platform(platform))
        except ValueError:
            pass
    if reason:
        try:
            query = query.filter(ReturnItem.scrap_reason == ScrapReason(reason))
        except ValueError:
            pass
    if q.strip():
        number = R.parse_label(q)
        if number is not None:
            query = query.filter(ReturnItem.id == number)
        else:
            like = f"%{q.strip()}%"
            uids = [p.uid_1c for p in db.query(Product).filter(
                Product.article.ilike(like)).all()]
            query = query.filter((ReturnItem.barcode.ilike(like))
                                 | (ReturnItem.uid_1c.in_(uids) if uids else False))
    return query.order_by(ReturnItem.status_changed_at.desc())


@router.get("/returns/scrapped", response_class=HTMLResponse)
def scrapped_page(request: Request, status: str = "", platform: str = "",
                  reason: str = "", q: str = "",
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = _scrap_query(db, status, platform, reason, q)
    rows = query.limit(LIST_LIMIT).all()

    # Сводка по причинам — ради неё страница и нужна. «Утилизировано 40» ничего
    # не решает, «из них 12 подмена» — повод для претензии площадке.
    by_reason = {}
    for pl, rs, n in (db.query(ReturnItem.platform, ReturnItem.scrap_reason,
                               func.count(ReturnItem.id))
                      .filter(ReturnItem.status.in_(SCRAP_VIEW))
                      .group_by(ReturnItem.platform, ReturnItem.scrap_reason).all()):
        by_reason[(pl, rs)] = n

    ctx = _base(request, user, db)
    ctx.update({"rows": rows, "products": _products(db, rows),
                "by_reason": by_reason, "reasons": list(ScrapReason),
                "platforms": list(Platform), "statuses": SCRAP_VIEW,
                "f_status": status, "f_platform": platform, "f_reason": reason,
                "q": q, "total": query.order_by(None).count(), "limit": LIST_LIMIT,
                "tasks": _scrap_tasks(db, rows),
                "responsible": R.scrap_responsible(db)})
    return templates.TemplateResponse(request, "returns_scrapped.html", ctx)


def _scrap_tasks(db: Session, rows) -> dict:
    """Номера документов 1С — ОДНИМ запросом на страницу, а не по строке."""
    ids = [r.ftp_task_id for r in rows if r.ftp_task_id]
    if not ids:
        return {}
    return {t.id: t for t in db.query(FtpTask).filter(FtpTask.id.in_(ids)).all()}


@router.post("/returns/scrapped/responsible")
def set_responsible(request: Request, name: str = Form(""),
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Сменить ответственного в документах списания 1С.

    Пустое поле ВОЗВРАЩАЕТ умолчание, а не оставляет пустоту: пустой
    ответственный — это отказ проведения на первой же утилизации, то есть
    человек сломал бы утилизацию, просто стерев поле.
    """
    from app import settings_store

    settings_store.set_value(db, R.SCRAP_RESPONSIBLE_SETTING, name.strip())
    log_action(db, user.username, "returns_scrap_responsible",
               name.strip() or f"(умолчание: {R.DEFAULT_SCRAP_RESPONSIBLE})")
    db.commit()
    set_flash(request, f"Ответственный в документах списания: "
                       f"{R.scrap_responsible(db)}.", "good")
    return RedirectResponse("/returns/scrapped", status_code=303)


@router.get("/returns/scrapped/export")
def scrapped_export(request: Request, status: str = "", platform: str = "",
                    reason: str = "", q: str = "",
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Выгрузка берёт ВЕСЬ отбор, а не показанную страницу.

    Файл для того и выгружают, чтобы разобрать пачкой — претензию площадке за
    подмену пишут по списку, а не по первым тремстам строкам. Отдай он страницу
    и промолчи об этом, претензия ушла бы неполной, и узнать об этом было бы
    неоткуда.
    """
    rows = _scrap_query(db, status, platform, reason, q).all()
    products = _products(db, rows)
    tasks = _scrap_tasks(db, rows)

    out = []
    for item in rows:
        product = products.get(item.uid_1c)
        task = tasks.get(item.ftp_task_id)
        out.append([
            R.label_number(item), R.RETURN_LABELS[item.status],
            PLATFORM_LABELS.get(item.platform, ""),
            R.SCRAP_LABELS.get(item.scrap_reason, ""),
            item.barcode,
            product.article if product else "", product.size if product else "",
            product.color if product else "", product.name if product else "",
            format_dt(item.created_at), format_dt(item.status_changed_at),
            (task.result_detail or "") if task and task.result_status == "OK" else "",
        ])
    return build_xlsx_response(SCRAP_EXPORT_HEADERS, out,
                               f"Утилизация_{today_local():%Y-%m-%d}.xlsx")


# Колонки файла утилизации. Номер опознаёт ВЕЩЬ, и только он: баркод опознаёт
# SKU, а три одинаковых свитшота дают три записи с одним баркодом — по нему
# нельзя понять, какую из них выбросили.
IMPORT_NUMBER = "Номер"
IMPORT_REASON = "Причина"

SCRAP_TEMPLATE_HEADERS = [IMPORT_NUMBER, IMPORT_REASON, "Баркод (справочно)",
                          "Артикул (справочно)", "Размер (справочно)",
                          "Статус (справочно)"]


@router.get("/returns/scrapped/template")
def scrap_template(request: Request, platform: str = "", q: str = "",
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Заготовка для массовой утилизации: вещи, ПО КОТОРЫМ РЕШЕНИЕ ЕЩЁ НЕ
    ПРИНЯТО, с пустой колонкой причины и выпадающим списком в ней.

    Заготовку выгружаем именно отсюда, а не заставляем человека собирать файл
    руками: номер RET он иначе перепишет с наклейки с опечаткой, а строка с
    неверным номером — это либо отказ, либо, хуже, утилизация чужой вещи.
    """
    query = db.query(ReturnItem).filter(
        ReturnItem.status.in_((ReturnStatus.accepted, ReturnStatus.cleaning,
                               ReturnStatus.repack, ReturnStatus.held,
                               ReturnStatus.rejected_1c)))
    if platform:
        try:
            query = query.filter(ReturnItem.platform == Platform(platform))
        except ValueError:
            pass
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(ReturnItem.barcode.ilike(like))
    rows = query.order_by(ReturnItem.created_at.desc()).limit(MAX_IMPORT_ROWS).all()
    products = _products(db, rows)

    out = []
    for item in rows:
        product = products.get(item.uid_1c)
        out.append([R.label_number(item), "", item.barcode,
                    product.article if product else "",
                    product.size if product else "",
                    R.RETURN_LABELS[item.status]])
    # Причина — ВЫПАДАЮЩИМ списком, а не набором руками: разбор идёт по точному
    # совпадению, и «Брак.» с точкой стал бы ошибкой строки, а «брак» в чужой
    # раскладке — ошибкой, которую человек не увидит глазами.
    choices = {IMPORT_REASON: [R.SCRAP_LABELS[r] for r in ScrapReason]}
    return build_xlsx_response(SCRAP_TEMPLATE_HEADERS, out,
                               f"Утилизация_заготовка_{today_local():%Y-%m-%d}.xlsx",
                               choices=choices)


@router.post("/returns/scrapped/import")
def scrap_import(request: Request, file: UploadFile = File(...),
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Массовая утилизация файлом — ТЕМ ЖЕ путём, что и кнопка в строке.

    Каждая строка идёт через `send_scrap_to_1c`, то есть через все проверки:
    статус, причину, наличие товара 1С. Массовый путь, делающий не то же самое,
    что построчный, — отдельный класс дефектов этого проекта, и стоил он уже
    дорого: там оверселл случался сразу по всему отбору.

    Пустая ячейка причины НИЧЕГО НЕ МЕНЯЕТ. Заготовка выгружает её пустой у
    КАЖДОЙ строки, то есть файл почти целиком состоит из пустых причин, и
    понимай мы пустоту как «утилизировать без причины» — один залитый файл
    отправил бы в 1С весь список.
    """
    by_reason = {R.SCRAP_LABELS[r].lower(): r for r in ScrapReason}
    try:
        raw = read_upload(file.file)
        rows = read_xlsx_rows(raw)
    except ExcelReadError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/returns/scrapped", status_code=303)

    sent, skipped, errors = 0, 0, []
    for line, row in enumerate(rows, start=2):
        number = R.parse_label(str(row.get(IMPORT_NUMBER) or ""))
        reason_cell = str(row.get(IMPORT_REASON) or "").strip()
        if number is None:
            if reason_cell:
                errors.append(f"строка {line}: номер не похож на RET-…")
            continue
        if not reason_cell:
            skipped += 1                       # пустая причина — не трогаем
            continue
        reason = by_reason.get(reason_cell.lower())
        if reason is None:
            errors.append(f"строка {line}: причина «{reason_cell}» не из списка")
            continue
        item = db.query(ReturnItem).filter(ReturnItem.id == number).first()
        if item is None:
            errors.append(f"строка {line}: {number} — такого возврата нет")
            continue
        try:
            R.send_scrap_to_1c(db, item, reason)
            sent += 1
        except R.ReturnError as e:
            errors.append(f"строка {line}: {R.label_number(item)} — {e}")

    log_action(db, user.username, "returns_scrap_import",
               f"отправлено {sent}, пропущено {skipped}, ошибок {len(errors)}")
    db.commit()

    # Итоги В НАЧАЛЕ: сообщение режется по `MAX_FLASH_CHARS` с конца, и потерять
    # примеры ошибок неприятно, а потерять итоги — значит оставить человека в
    # уверенности, что файл применился целиком.
    message = (f"Отправлено на утилизацию: {sent}. "
               f"Строк без причины пропущено: {skipped}.")
    if errors:
        message += f" Ошибок: {len(errors)}. " + "; ".join(errors[:5])
    set_flash(request, message, "warn" if errors else "good")
    return RedirectResponse("/returns/scrapped", status_code=303)


# ------------------------------------------------------------------ наклейка

@router.get("/returns/item/{item_id}/label", response_class=HTMLResponse)
def label(request: Request, item_id: int, db: Session = Depends(get_db),
          user=Depends(get_current_user)):
    """Страница ровно в размер этикетки — её печатает браузер складской машины.

    Печатать с сервера нельзя: принтер подключён по USB и виден только внутри
    сессии пользователя, а веб-служба работает под NSSM без интерактивной
    сессии и такой очереди не видит вовсе. Это тот же случай, что с клиентом
    облака, уже описанный в `CLAUDE.md`: печать молча переставала бы работать
    при выходе из RDP — отказ, неотличимый от исправной тишины.
    """
    item = db.query(ReturnItem).filter(ReturnItem.id == item_id).first()
    if item is None:
        return HTMLResponse("<p>Возврат не найден</p>", status_code=404)
    product = (db.query(Product).filter(Product.uid_1c == item.uid_1c).first()
               if item.uid_1c else None)
    number = R.label_number(item)
    return templates.TemplateResponse(request, "returns_label.html", {
        "request": request, "item": item, "product": product, "number": number,
        "barcode": barcode_svg(number),
        # Дата на наклейке — КАЛЕНДАРНОЕ число, а его называет человек: у
        # Москвы UTC+3, и у вещи, принятой до трёх ночи, UTC-шное число
        # вчерашнее. Наклейку потом сверяют с коробкой и накладной ПВЗ глазами,
        # и расхождение в день объясняют чем угодно, кроме часового пояса.
        # Отметки времени на страницах остаются UTC-шными, как и во всей
        # админке, — здесь именно дата.
        "accepted_on": local_date_of(item.created_at),
        "platform_label": PLATFORM_LABELS.get(item.platform, ""),
    })


# --------------------------------------------------------- массовое решение

@router.get("/returns/bulk", response_class=HTMLResponse)
def bulk_page(request: Request, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    """Коробка целиком: сканируем вещи, потом ОДНОЙ кнопкой отправляем в 1С.

    Зачем отдельный экран, когда решение есть на странице вещи. Затем, что
    коробка из ПВЗ — это десятки вещей, и открывать по каждой её страницу
    означает десятки переходов там, где человек делает одно и то же движение.
    Разбор по одной остаётся для спорных: там решение принимают, глядя на вещь,
    и подсказки на той странице про это.

    Действие и причина выбираются РАНЬШЕ сканов и видны всё время. Причина одна
    на всю пачку — так просил склад, и это честно: коробку разбирают под одну
    задачу. Но именно поэтому она крупная на экране: уехав не с той причиной,
    вещи лягут не на ту статью затрат в 1С, а претензию площадке пишут по этому
    полю, и увидят расхождение в отчётах через месяц.
    """
    items = R.batch_entries(db)
    warehouses = R.warehouse_choices()
    warehouse = request.session.get("returns_bulk_warehouse") or ""
    ctx = _base(request, user, db)
    ctx.update({
        "batch": items, "products": _products(db, items),
        "totals": R.batch_totals(db, items, warehouse),
        "mode": request.session.get("returns_bulk_mode") or "sale",
        "reason": request.session.get("returns_bulk_reason") or "",
        "warehouse": warehouse, "warehouses": warehouses,
        "warehouse_platform": R.platform_of_warehouse(warehouse),
        "scrap_reasons": list(ScrapReason),
        "max_batch": R.MAX_BATCH, "target_warehouse": R.TARGET_WAREHOUSE,
    })
    return templates.TemplateResponse(request, "returns_bulk.html", ctx)


@router.post("/returns/bulk/mode")
def bulk_mode(request: Request, mode: str = Form("sale"), reason: str = Form(""),
              warehouse: str = Form(""), user=Depends(get_current_user)):
    """Что делаем с пачкой и, для утиля, по какой причине.

    Живёт в сессии браузера, а не в базе: это не состояние установки, а выбор
    текущего человека за текущей коробкой, и число тут одно — потерять его
    вместе с cookie не страшно, страницу видно целиком.
    """
    request.session["returns_bulk_mode"] = "scrap" if mode == "scrap" else "sale"
    request.session["returns_bulk_reason"] = reason if reason else ""
    # Склад принимаем ТОЛЬКО из карты: имя уезжает в 1С строкой, и чужое там
    # означало бы документ, который либо не проведётся, либо вернёт товар не
    # оттуда. Незнакомое значит «не выбран», а не «запишем как есть».
    request.session["returns_bulk_warehouse"] = (
        warehouse if R.platform_of_warehouse(warehouse) else "")
    return RedirectResponse("/returns/bulk", status_code=303)


@router.post("/returns/bulk/scan")
def bulk_scan(request: Request, code: str = Form(""), db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    """Скан кладёт вещь в пачку. Статус её при этом НЕ меняется.

    До «Передать в 1С» не случилось ничего: вынуть строку обратно ничего не
    стоит, и передумать можно всей коробкой. Обратный порядок (скан сразу
    отправляет) выглядит быстрее и платит хуже — ошибившись причиной на сороковой
    вещи, человек отменял бы сорок отправок по одной.
    """
    code = (code or "").strip()
    # `RET-…` со старой наклейки тоже принимаем: человек сканирует то, что видит
    # на вещи, и отказ «не тот формат» здесь ничему не учит.
    number = R.parse_label(code)
    to_scrap = (request.session.get("returns_bulk_mode") or "sale") == "scrap"
    target = ReturnStatus.awaiting_scrap if to_scrap else ReturnStatus.awaiting_1c
    try:
        if number is not None:
            item = db.query(ReturnItem).filter(ReturnItem.id == number).first()
            if item is None:
                raise R.ReturnError(f"Возврата {code} нет")
            if item.id in R.in_batch(db):
                raise R.ReturnError(f"{code} уже в пачке")
            if not R.can_change(item, target):
                raise R.ReturnError(f"{code}: по этой вещи решение уже принято "
                                    f"({R.RETURN_LABELS[item.status]})")
        else:
            item = R.pick_for_batch(db, code, target)
        R.add_to_batch(db, item)
    except R.ReturnError as e:
        set_flash(request, str(e), "warn")
        return RedirectResponse("/returns/bulk", status_code=303)
    db.commit()
    return RedirectResponse("/returns/bulk", status_code=303)


@router.post("/returns/bulk/drop/{item_id}")
def bulk_drop(request: Request, item_id: int, db: Session = Depends(get_db),
              user=Depends(get_current_user)):
    R.drop_from_batch(db, item_id)
    db.commit()
    return RedirectResponse("/returns/bulk", status_code=303)


@router.post("/returns/bulk/clear")
def bulk_clear(request: Request, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    removed = R.clear_batch(db)
    db.commit()
    set_flash(request, f"Пачка очищена, вещей убрано: {removed}. "
                       f"Их статусы не менялись — они остались в разборе.", "info")
    return RedirectResponse("/returns/bulk", status_code=303)


@router.post("/returns/bulk/send")
def bulk_send(request: Request, mode: str = Form("sale"), reason: str = Form(""),
              warehouse: str = Form(""),
              db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Передать пачку в 1С — тем же путём, что и кнопка на странице вещи.

    Действие и причина приходят ФОРМОЙ, а не из сессии: между выбором и нажатием
    человек мог поменять их в соседнем окне (пачка одна на установку), и решать
    судьбу сорока вещей по значению, которого он сейчас не видит, нельзя.
    """
    to_scrap = mode == "scrap"
    items = R.batch_entries(db)
    if not items:
        set_flash(request, "Пачка пуста — сканировать нечего.", "warn")
        return RedirectResponse("/returns/bulk", status_code=303)

    scrap_reason = None
    if to_scrap:
        try:
            scrap_reason = ScrapReason(reason) if reason else None
        except ValueError:
            scrap_reason = None
        if scrap_reason is None:
            set_flash(request, "У утилизации обязательна причина: по ней 1С "
                               "выбирает хоз. операцию списания, а площадке "
                               "пишут претензию.", "warn")
            return RedirectResponse("/returns/bulk", status_code=303)

    if not R.platform_of_warehouse(warehouse):
        set_flash(request, "Выберите склад, с которого пришла коробка: с него 1С "
                           f"вернёт вещи на «{R.TARGET_WAREHOUSE}», и только "
                           "оттуда.", "warn")
        return RedirectResponse("/returns/bulk", status_code=303)

    result = R.send_batch(db, items, to_scrap, scrap_reason, warehouse)
    if result["wrong_warehouse"]:
        # Отказ ВСЕЙ пачке, и строки названы поимённо: вопрос тут не про две
        # лишние вещи, а про то, что коробка собрана не с того склада.
        names = ", ".join(R.label_number(i) for i in result["wrong_warehouse"][:5])
        more = (f" и ещё {len(result['wrong_warehouse']) - 5}"
                if len(result["wrong_warehouse"]) > 5 else "")
        set_flash(request, f"Не передано НИЧЕГО: склад «{warehouse}», а эти вещи "
                           f"приняты с другой площадки — {names}{more}. Уберите их "
                           f"из пачки или выберите их склад.", "warn")
        return RedirectResponse("/returns/bulk", status_code=303)
    what = (f"утилизация, «{R.SCRAP_LABELS[scrap_reason]}»" if to_scrap
            else "возврат в продажу")
    log_action(db, user.username, "returns_bulk_send",
               f"{what} со склада «{warehouse}»: передано {result['sent']}, "
               f"отказов {len(result['failed'])}")
    db.commit()

    if result["failed"]:
        # Отказавшие остаются в пачке со своей причиной: молча потерять их
        # значило бы оставить человека в уверенности, что коробка передана вся.
        examples = "; ".join(f"{R.label_number(i)} — {why}"
                             for i, why in result["failed"][:3])
        set_flash(request, f"Передано в 1С: {result['sent']}. Осталось в пачке "
                           f"{len(result['failed'])} — их не приняли: {examples}",
                  "warn")
    else:
        set_flash(request, f"Передано в 1С: {result['sent']} ({what}). "
                           f"1С перенесёт вещи «{warehouse}» → "
                           f"«{R.TARGET_WAREHOUSE}»; статус поставит её ответ.",
                  "good")
    return RedirectResponse("/returns/bulk", status_code=303)
