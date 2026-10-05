"""Страница «Коды ЧЗ» поставки: заказ в СУЗ, получение, статусы, ввод в оборот,
этикетки и файл кодов.

Страница только подписывает (плагин КриптоПро), всё остальное делает
программа: JSON-вызовы ниже отдают строку для подписи и принимают подпись.
"""
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from markapp import audit, chz_auth, codes as C, codes_txt, labels as L
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import CodeOrder, IntroduceDoc, Supply, User
from markapp.pages import render
from markapp.timeutils import ru, today_local

router = APIRouter()


def _err(text: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status)


def _back(supply_id: int) -> RedirectResponse:
    return RedirectResponse(f"/supplies/{supply_id}/codes", status_code=303)


@router.get("/supplies/{supply_id}/codes")
def codes_page(supply_id: int, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    changed = C.expire_sending(db)
    recovered = C.recover_journal(db)
    if changed or recovered:
        if recovered:
            audit.log(db, user.username, "codes_recovered", f"поставка {supply_id}",
                      f"восстановлено из журнала кодов: {recovered}")
        db.commit()
    lines = C.plan(db, supply)
    org = supply.organization
    return render(request, "codes.html", user, "supplies", supply=supply, org=org, lines=lines,
                  total=C.summary(lines), orders=db.query(CodeOrder).filter(CodeOrder.supply_id == supply.id)
                  .order_by(CodeOrder.id.desc()).all(),
                  docs=db.query(IntroduceDoc).filter(IntroduceDoc.supply_id == supply.id)
                  .order_by(IntroduceDoc.id.desc()).all(),
                  ready=len(C.ready_codes(db, supply)), steps=len(C.steps(db, supply)),
                  can_order=supply.status in C.ORDER_STATUSES and not supply.is_test,
                  chz_ok=chz_auth.token(org) is not None, suz_ok=chz_auth.token(org, "suz") is not None,
                  intro=C.defaults(supply), cert_types=C.CERT_TYPES, backup=C.backup_state(db, supply),
                  status_ru=C.STATUS_RU, order_ru=C.ORDER_RU, doc_ru=C.DOC_RU,
                  today=today_local().isoformat())


@router.post("/supplies/{supply_id}/codes/order-prepare")
def order_prepare(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return _err("поставка не найдена", 404)
    try:
        C.suz_token(supply)
        orders = C.prepare_orders(db, supply, user.username)
    except C.CodesError as e:
        db.rollback()
        return _err(str(e))
    db.commit()
    return {"orders": [{"id": o.id, "sku": o.supplier_sku, "quantity": o.quantity, "body": o.body}
                       for o in orders]}


@router.post("/supplies/{supply_id}/codes/order-send")
def order_send(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
               order_id: int = Body(...), signature: str = Body(...)):
    order = db.get(CodeOrder, order_id)
    if order is None or order.supply_id != supply_id:
        return _err("заказ не найден", 404)
    try:
        C.send_order(db, order, signature)
    except C.CodesError as e:
        if order.status in ("unknown", "error"):
            audit.log(db, user.username, "codes_order_failed", order.supplier_sku,
                      f"поставка {supply_id}, {order.quantity} шт.: {order.status} — {e}")
        db.commit()                      # итог (unknown / error / new) остаётся у заказа
        return _err(f"{order.supplier_sku}: {e}", 502)
    audit.log(db, user.username, "codes_order", order.supplier_sku,
              f"поставка {order.supply_id}, GTIN {order.gtin}, {order.quantity} шт., СУЗ {order.suz_order_id}")
    db.commit()
    return {"ok": True, "suz_order_id": order.suz_order_id}


@router.post("/supplies/{supply_id}/codes/order-resolve")
def order_resolve(supply_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user), order_id: int = Form(...),
                  suz_order_id: str = Form("")):
    order = db.get(CodeOrder, order_id)
    if order is None or order.supply_id != supply_id:
        return _back(supply_id)
    try:
        C.resolve_unknown_order(db, order, suz_order_id)
    except C.CodesError as e:
        flash(request, str(e), "error")
        return _back(supply_id)
    audit.log(db, user.username, "codes_order_resolved", order.supplier_sku,
              f"поставка {supply_id}: {order.error} {order.suz_order_id}")
    db.commit()
    flash(request, "Решение по заказу записано.", "ok")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/codes/steps")
def codes_steps(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return _err("поставка не найдена", 404)
    return {"steps": C.steps(db, supply)}


@router.post("/supplies/{supply_id}/codes/step")
def codes_step(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
               order_id: int = Body(...), action: str = Body(...), path: str = Body(...),
               signature: str = Body(...)):
    order = db.get(CodeOrder, order_id)
    if order is None or order.supply_id != supply_id:
        return _err("заказ не найден", 404)
    before = order.received
    try:
        C.run_step(db, order, action, path, signature)
    except C.CodesError as e:
        db.commit()
        return _err(f"{order.supplier_sku}: {e}", 502)
    got = order.received - before
    if got > 0:
        audit.log(db, user.username, "codes_received", order.supplier_sku,
                  f"поставка {order.supply_id}, +{got}, всего {order.received}/{order.quantity}")
    db.commit()
    if got > 0:
        C.request_backup()               # после коммита: копия должна видеть эти коды
    return {"ok": True, "status": order.status, "received": order.received, "quantity": order.quantity,
            "error": order.error}


@router.post("/supplies/{supply_id}/codes/intro-prepare")
def intro_prepare(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return _err("поставка не найдена", 404)
    try:
        doc = C.prepare_introduce(db, supply, user.username)
    except C.CodesError as e:
        db.commit()
        return _err(str(e))
    db.commit()
    return {"doc_id": doc.id, "document": doc.document, "count": doc.codes_count,
            "summary": getattr(doc, "summary", "")}


@router.post("/supplies/{supply_id}/codes/intro-send")
def intro_send(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
               doc_id: int = Body(...), signature: str = Body(...)):
    doc = db.get(IntroduceDoc, doc_id)
    if doc is None or doc.supply_id != supply_id:
        return _err("документ не найден", 404)
    try:
        C.send_introduce(db, doc, signature)
    except C.CodesError as e:
        audit.log(db, user.username, "codes_introduce_failed", f"поставка {supply_id}",
                  f"{doc.codes_count} кодов: {doc.status} — {e}")
        db.commit()
        return _err(str(e), 502)
    audit.log(db, user.username, "codes_introduce", f"поставка {supply_id}",
              f"{doc.codes_count} кодов, документ {doc.doc_id or 'без номера'}")
    db.commit()
    return {"ok": True, "doc_id": doc.doc_id}


@router.post("/supplies/{supply_id}/codes/doc-release")
def doc_release(supply_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user), doc_id: int = Form(...)):
    doc = db.get(IntroduceDoc, doc_id)
    if doc is None or doc.supply_id != supply_id:
        return _back(supply_id)
    try:
        C.release_unknown_doc(db, doc)
    except C.CodesError as e:
        flash(request, str(e), "error")
        return _back(supply_id)
    audit.log(db, user.username, "codes_doc_released", f"поставка {supply_id}", f"документ #{doc.id}")
    db.commit()
    flash(request, "Коды документа освобождены — их можно отправить новым документом.", "ok")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/codes/refresh")
def codes_refresh(supply_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    stats = C.refresh_statuses(db, supply, limit=2)
    C.refresh_documents(db)
    db.commit()
    flash(request, stats["note"] or f"Статусы обновлены: запросов {stats['requests']}, изменилось {stats['updated']}.",
          "warn" if stats["note"] else "ok")
    return _back(supply_id)


@router.post("/supplies/{supply_id}/codes/attrs")
def codes_attrs(supply_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user), tnved: str = Form(""), cert_type: str = Form(""),
                cert_number: str = Form(""), cert_date: str = Form(""), production_date: str = Form("")):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    try:
        C.save_defaults(supply, tnved, cert_type, cert_number, cert_date, production_date)
    except C.CodesError as e:
        flash(request, f"Не сохранено: {e}", "error")
        return _back(supply_id)
    audit.log(db, user.username, "codes_intro_attrs", f"поставка {supply_id}",
              f"ТН ВЭД {tnved}, {cert_type} {cert_number} от {cert_date}, производство {production_date}")
    db.commit()
    flash(request, "Данные документа сохранены.", "ok")
    return _back(supply_id)


def _file(data: bytes, name: str, media: str) -> Response:
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.post("/supplies/{supply_id}/codes/labels")
def codes_labels(supply_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """Этикетки по кодам поставки из реестра. Печать — по желанию (ТЗ 7.3):
    ни ввод в оборот, ни файл от неё не зависят."""
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    full = C.full_codes(db, supply)
    if not full:
        flash(request, "У поставки ещё нет полученных кодов.", "error")
        return _back(supply_id)
    data = L.label_values(db, full, supply.organization, ru(today_local()), supply.number)
    try:
        pdf, warnings = L.build_pdf(db, data)
    except L.LabelError as e:
        flash(request, f"Этикетки не выданы: {e}", "error")
        return _back(supply_id)
    audit.log(db, user.username, "labels_printed", f"поставка {supply.number}",
              f"{len(full)} этикеток из реестра; предупреждений {len(warnings)}")
    db.commit()
    return _file(pdf, f"Этикетки_{supply.number}_{len(full)}шт.pdf", "application/pdf")


@router.post("/supplies/{supply_id}/codes/txt")
def codes_file(supply_id: int, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Коды поставки в txt, как выдаёт СУЗ (ТЗ 6.3). Только когда ВСЕ коды поставки
    в обороте и их ровно столько, сколько штук в поставке."""
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    total = C.summary(C.plan(db, supply))
    try:
        if total["codes"] != total["need"]:
            raise C.CodesError(f"кодов {total['codes']}, а штук в поставке {total['need']}")
        data = codes_txt.build(C.full_codes(db, supply, only_introduced=True))
    except (C.CodesError, codes_txt.CodesTxtError) as e:
        flash(request, f"Файл кодов не выдан: {e}", "error")
        return _back(supply_id)
    audit.log(db, user.username, "codes_txt", f"поставка {supply.number}", f"{total['codes']} кодов")
    db.commit()
    return _file(data, codes_txt.filename(supply.doc_number), "text/plain; charset=ascii")
