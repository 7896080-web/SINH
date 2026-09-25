"""Возвраты: приёмка, разбор, список, печать наклейки.

Три экрана, а не одна таблица, потому что у них разные задачи и разный темп.
Приёмка — одно поле и сканер, кладовщик смотрит на экран краем глаза. Разбор —
одна вещь крупно и решение по ней. Список — то, что осталось доделать.

Площадка выбирается КОРОБКОЙ и живёт в сессии: кладовщик едет в конкретный ПВЗ и
привозит возвраты одной площадки, поэтому спрашивать её на каждый скан незачем —
это и медленнее, и ошибочнее. Полоса с названием и счётчиком висит на экране всё
время: забыть, под какой площадкой принимаешь, должно быть трудно.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import returns as R
from app.audit import log_action
from app.barcode128 import svg as barcode_svg
from app.database import get_db
from app.dependencies import get_current_user
from app.flash import pop_flash, set_flash
from app.models import (Platform, Product, ReturnItem, ReturnItemLog, ReturnStatus,
                        ScrapReason)
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
            "hints": R.RETURN_HINTS,
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
