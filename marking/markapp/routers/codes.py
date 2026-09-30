"""Страница «Коды ЧЗ» поставки: заказ в СУЗ, получение, статусы, ввод в оборот.

Страница только подписывает (плагин КриптоПро), всё остальное делает
программа: JSON-вызовы ниже отдают строку для подписи и принимают подпись.
"""
from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from markapp import audit, chz_auth, codes as C
from markapp.database import get_db
from markapp.deps import get_current_user
from markapp.flash import flash
from markapp.models import CodeOrder, IntroduceDoc, Supply, User
from markapp.pages import render

router = APIRouter()


def _err(text: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": text}, status_code=status)


@router.get("/supplies/{supply_id}/codes")
def codes_page(supply_id: int, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
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
                  intro=C.defaults(supply), cert_types=C.CERT_TYPES,
                  status_ru=C.STATUS_RU, order_ru=C.ORDER_RU)


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
        db.commit()                      # текст ошибки остаётся у заказа
        return _err(f"{order.supplier_sku}: {e}", 502)
    audit.log(db, user.username, "codes_order", order.supplier_sku,
              f"поставка {order.supply_id}, GTIN {order.gtin}, {order.quantity} шт., СУЗ {order.suz_order_id}")
    db.commit()
    return {"ok": True, "suz_order_id": order.suz_order_id}


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
    if order.received > before:
        audit.log(db, user.username, "codes_received", order.supplier_sku,
                  f"поставка {order.supply_id}, +{order.received - before}, всего {order.received}/{order.quantity}")
    db.commit()
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
    return {"doc_id": doc.id, "document": doc.document, "count": doc.codes_count}


@router.post("/supplies/{supply_id}/codes/intro-send")
def intro_send(supply_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
               doc_id: int = Body(...), signature: str = Body(...)):
    doc = db.get(IntroduceDoc, doc_id)
    if doc is None or doc.supply_id != supply_id:
        return _err("документ не найден", 404)
    try:
        C.send_introduce(db, doc, signature)
    except C.CodesError as e:
        db.commit()
        return _err(str(e), 502)
    audit.log(db, user.username, "codes_introduce", f"поставка {supply_id}",
              f"{doc.codes_count} кодов, документ {doc.doc_id}")
    db.commit()
    return {"ok": True, "doc_id": doc.doc_id}


@router.post("/supplies/{supply_id}/codes/refresh")
def codes_refresh(supply_id: int, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    supply = db.get(Supply, supply_id)
    if supply is None:
        return RedirectResponse("/supplies", status_code=303)
    stats = C.refresh_statuses(db, supply, limit=5)
    C.refresh_documents(db)
    db.commit()
    flash(request, stats["note"] or f"Статусы обновлены: запросов {stats['requests']}, изменилось {stats['updated']}.",
          "warn" if stats["note"] else "ok")
    return RedirectResponse(f"/supplies/{supply_id}/codes", status_code=303)


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
        return RedirectResponse(f"/supplies/{supply_id}/codes", status_code=303)
    audit.log(db, user.username, "codes_intro_attrs", f"поставка {supply_id}",
              f"ТН ВЭД {tnved}, {cert_type} {cert_number} от {cert_date}, производство {production_date}")
    db.commit()
    flash(request, "Данные документа сохранены.", "ok")
    return RedirectResponse(f"/supplies/{supply_id}/codes", status_code=303)
